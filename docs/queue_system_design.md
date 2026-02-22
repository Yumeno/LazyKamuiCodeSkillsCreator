# MCP リクエスト・キューイングシステム設計書

> **📖 補足**: 本設計書はキューシステムの初期設計を記述しています。
> 以下の機能拡張については [堅牢化設計書 (queue_system_hardening.md)](queue_system_hardening.md) を参照してください:
> - ゾンビジョブ回復（recovering ステータス）
> - 指数バックオフリトライ（ConnectionError/503/504/429）
> - ワーカー側ダウンロード
> - 共有キューディレクトリ（`.claude/queue/`）
> - 古いジョブの自動削除
> - queue_config.json のマージ保護

## 1. 目的

現在の LazyKamuiCodeSkillsCreator は、AIエージェント（Claude Code等）から呼び出された際、直接対象のMCPサーバーに対して非同期ジョブ（Submit → Statusポーリング → Result）を実行します。

しかし、AIが複数ツールを並列かつ高速に呼び出した場合、サーバーへの同時アクセスが急増し、過剰な負荷やレートリミット（429 Too Many Requests）エラーを引き起こす問題があります。

本プロジェクトの目的は、**クライアントと外部MCPサーバーの間にローカルのキューイング層（ワーカープロセス）を設け、リクエストの並行数と実行間隔を制御すること**です。

さらに、複数の異なるMCPサーバーを利用する場合でも、**「宛先（エンドポイント）ごとに独立したキューと、個別の制限値」**を持たせることで、重い処理を行うサーバーに引きずられて他の軽量なサーバーが待たされること（Head-of-Lineブロッキング）を防ぎます。

同時に、AIエージェントの利用体験を損なわないよう「ワーカーの完全な自動起動・自動終了」を実現し、ユーザーがデーモンプロセスを意識せずに済むアーキテクチャを構築します。

## 2. 要件定義

### 2.1. 機能要件

1. **エンドポイントごとのレートリミットと並行数制御**: 外部MCPサーバーへの負荷を防ぐため、「同時実行数の上限」および「ジョブ実行の最低間隔」を設定します。これらはデフォルト値を持つだけでなく、**エンドポイント（URL）ごとに個別の値を設定**して柔軟に制限を制御できること。
2. **ノンブロッキングなジョブ投入と状態確認**: クライアントはジョブ登録（submit）と状態確認（wait）を分離し、投入後即座に `job_id` を受け取れること。状態確認も即座にレスポンスを返すステートレスAPIであること。
3. **オンデマンド自動起動**: ワーカープロセスが起動していない場合、クライアントは接続エラーを検知し、自動的にバックグラウンドでワーカーを起動してからリクエストを再送すること。
4. **自動終了（アイドルタイムアウト）**: ワーカーはキューが空になり、一定時間（例: 60秒）新たなリクエストがない場合、自動的に終了してリソースを解放すること。
5. **ポートの可変性**: ワーカーが使用するHTTPポートは固定せず、設定ファイル（または環境変数）から変更可能であること。
6. **ジョブの永続化**: 処理状況と結果をSQLiteに保存し、ワーカークラッシュ時でも状態を保護すること。
7. **クライアントの3つの動作モード**: `--submit-only`（投入のみ）、`--wait`（状態確認のみ）、`--blocking`（従来互換の完了待ち）を提供すること。

### 2.2. 非機能要件

1. **標準ライブラリの利用**: 原則としてPython標準ライブラリ（sqlite3, http.server, threading, subprocess 等）のみを使用し、外部依存を増やさないこと。ただし外部MCPサーバーとの通信に `requests` ライブラリを使用する（既存依存）。
2. **OS非依存（クロスプラットフォーム対応）**: Windows、macOS、Linuxのいずれの環境でも同一の挙動を保証すること。特にバックグラウンドプロセス起動時において、OSに応じた適切な分離処理（黒窓回避、セッション切り離し等）を行うこと。
3. **SQLiteロック回避**: ワーカーのHTTPサーバーはステートレスに即座にレスポンスを返す設計とし、DBのロック競合を最小限に抑えること。

## 3. 基本設計（アーキテクチャ）

**「ローカル・ステートレスHTTP API ＋ SQLite」** 方式を採用します。ワーカープロセスはシステム全体で1つ（シングルトン）で、内部のディスパッチャがエンドポイントごとに設定された制限値を読み取り、タスクを振り分けます。

ワーカーのHTTPサーバーは**すべてのリクエストに即座にレスポンスを返すステートレスなREST API**に徹します。ロングポーリングやインメモリEventは使用しません。ジョブの完了待ちはクライアント側のポーリングで行います。

### 3.1. コンポーネント構成

1. **Client (mcp_async_call.py)**
   * ローカルの Worker に対してHTTPリクエストを送信します。
   * `--submit-only`: ジョブ登録して `job_id` を返して即終了。
   * `--wait JOB_ID`: ジョブの現在状態を1回確認して即返却。
   * `--blocking`: submit → wait ポーリング → ダウンロードまで一括実行（従来互換）。
   * ConnectionError 発生時は Worker をバックグラウンド起動し、ヘルスチェック通過後にリクエストを再送します。
2. **Worker (mcp_worker_daemon.py)**
   * ローカル（127.0.0.1）で待ち受ける HTTP サーバー（`http.server.HTTPServer`）。
   * 受け取ったジョブをSQLiteに保存し、バックグラウンドのディスパッチャが**宛先ごとの設定値に基づいて実行可能か判断**してスレッドプールへ送ります。
   * ジョブ完了時、結果（リモートURL等）をDBに書き込みます。ファイルダウンロードはワーカーでは行いません。
3. **Database (jobs.db)**
   * SQLiteデータベース。ジョブの状態・結果・セッション情報を永続化します。
4. **Config (queue_config.json)**
   * 各スキルディレクトリに配置。ポート番号やレートリミット設定を定義するファイル。

### 3.2. データフロー

```
[Client]                                   [Worker Daemon (Singleton)]             [SQLite]        [外部MCP]
   │                                              │                                  │                │
   ├── 1. POST /api/jobs (接続失敗)               │                                  │                │
   ├── 2. 起動(subprocess.Popen) ────────────────>│ (ポートバインド＆初期化)          │                │
   │                                              │                                  │                │
   ├── 3. POST /api/jobs (成功) ─────────────────>│──(INSERT)──────────────────────>│                │
   │<─ 3a. 200 OK {"job_id": "xxx"} ─────────────┤                                  │                │
   │                                              │                                  │                │
   │   (クライアント: 即座に返却 or ポーリング開始)│──(Dispatcher Loop)               │                │
   │                                              │   ├── Endpoint Aの処理可否判定   │                │
   │                                              │   ├── Endpoint Bの処理可否判定   │                │
   │                                              │   └── running に UPDATE ─────────>│                │
   │                                              │   └── 外部MCPサーバーへSubmit ───────────────────>│
   │                                              │                                  │                │
   │                                              │   (ステータスポーリング) <────────────────────────┤
   │                                              │                                  │                │
   │                                              │──(UPDATE: completed & result)──>│                │
   │                                              │                                  │                │
   ├── 4. GET /api/jobs/xxx (ポーリング) ────────>│──(SELECT)──────────────────────>│                │
   │<─ 4a. 200 OK {"status":"running"} ──────────┤                                  │                │
   │   (sleep 2秒)                                │                                  │                │
   ├── 5. GET /api/jobs/xxx (ポーリング) ────────>│──(SELECT)──────────────────────>│                │
   │<─ 5a. 200 OK {"status":"completed", ...} ───┤                                  │                │
   │                                              │                                  │                │
   │   (クライアント側でリモートURLからダウンロード)│                                  │                │
```

### 3.3. クライアントの3つの動作モード

AIエージェントがジョブをノンブロッキングに制御できるよう、クライアントは3つのモードを提供します。

#### `--submit-only` モード
ジョブを登録し、`job_id` を stdout に出力して即座に終了します。AIエージェントは複数のジョブを並列に投入し、後からまとめて結果を回収できます。

```bash
$ python mcp_async_call.py --submit-only --endpoint http://... --submit-tool generate_image --args '{"prompt":"cat"}'
{"job_id": "abc-123", "status": "pending"}
```

#### `--wait JOB_ID` モード
指定されたジョブの現在状態を問い合わせ、即座に結果を返して終了します。

```bash
$ python mcp_async_call.py --wait abc-123
{"job_id": "abc-123", "status": "running"}

$ python mcp_async_call.py --wait abc-123
{"job_id": "abc-123", "status": "completed", "result": {"urls": ["https://..."]}}
```

#### `--blocking` モード（デフォルト、従来互換）
内部的に `--submit-only` → `--wait` ポーリングループ → ダウンロードを一括実行します。既存の呼び出し方との後方互換性を維持します。

```bash
$ python mcp_async_call.py --endpoint http://... --submit-tool generate_image --args '{"prompt":"cat"}'
# （完了まで待機してからダウンロード結果を出力）
```

## 4. 詳細設計

### 4.1. 設定ファイル (queue_config.json)

各スキルディレクトリに配置します。エンドポイントごとに異なる制限値を定義できるよう、`default_rate_limit` と `endpoint_rate_limits` を分離します。

```json
{
  "host": "127.0.0.1",
  "port": 54321,
  "idle_timeout_seconds": 60,
  "default_rate_limit": {
    "max_concurrent_jobs": 2,
    "min_interval_seconds": 2.0
  },
  "endpoint_rate_limits": {
    "http://localhost:8000": {
      "max_concurrent_jobs": 1,
      "min_interval_seconds": 10.0
    },
    "http://192.168.1.50:8001": {
      "max_concurrent_jobs": 5,
      "min_interval_seconds": 0.5
    }
  },
  "job_retention_seconds": 86400,
  "results_dir": ".claude/queue/results"
}
```

> **注**: `job_retention_seconds` と `results_dir` は堅牢化で追加されたフィールドです。

### 4.2. SQLite スキーマ (jobs.db)

スキーマレス（パススルー）なアプローチにより、ペイロード（args）をJSON文字列として格納し、外部MCPサーバーの任意の引数構造を吸収します。セッションIDはジョブごとに保存し、ワーカークラッシュ後もステータス確認・結果取得を継続可能にします。

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    submit_tool TEXT NOT NULL,
    args TEXT NOT NULL,
    status_tool TEXT,
    result_tool TEXT,
    headers TEXT,
    session_id TEXT,              -- MCP セッションID（ジョブごとに保存）
    remote_job_id TEXT,           -- 外部MCPサーバーが返したジョブID
    status TEXT DEFAULT 'pending', -- pending, running, polling, completed, failed
    result TEXT,                  -- 完了時の結果（JSON文字列、リモートURL等を含む）
    error TEXT,                   -- 失敗時のエラー情報
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**ステータス遷移:**
```
pending → running → polling → completed
                             → failed

(ワーカー再起動時)
running  → failed       (submit未完了)
polling  → recovering   (remote_job_id有り: 回復可能)
polling  → failed       (remote_job_id無し: 回復不可)
recovering → polling    (Dispatcherが回復ジョブを再開)
```
- `pending`: キューで待機中
- `running`: 外部MCPサーバーにsubmit中
- `polling`: submit完了、ステータスポーリング中
- `recovering`: ワーカー再起動後の回復待ち（※堅牢化で追加）
- `completed`: 処理完了（結果あり）
- `failed`: 処理失敗（エラー情報あり）

### 4.3. Worker API (HTTP REST)

ワーカーのHTTPサーバーは**すべてのリクエストに即座にレスポンスを返す**ステートレスなAPIです。

* **GET /api/health**
  死活監視および起動完了確認。
  ```json
  {"status": "ok", "active_jobs": 3, "pending_jobs": 5}
  ```

* **POST /api/jobs**
  ジョブの登録。リクエストボディにジョブ情報を含め、即座に `job_id` を返します。
  ```json
  // Request
  {
    "endpoint": "http://mcp-server:8000/sse",
    "submit_tool": "generate_image",
    "args": {"prompt": "a cat"},
    "status_tool": "check_status",
    "result_tool": "get_result",
    "headers": {"Authorization": "Bearer xxx"}
  }
  // Response
  {"job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending"}
  ```

* **GET /api/jobs/{job_id}**
  ジョブの現在状態をDBから取得して即返却。
  ```json
  // 処理中
  {"job_id": "...", "status": "polling", "remote_job_id": "rj-456"}
  // 完了
  {"job_id": "...", "status": "completed", "result": {"urls": ["https://..."]}}
  // 失敗
  {"job_id": "...", "status": "failed", "error": "429 Too Many Requests"}
  ```

### 4.4. 内部キューイングとレートリミット（エンドポイント別）

Workerのバックグラウンドディスパッチャは以下のロジックで稼働し、**各MCPサーバーの固有の制限値に従って**独立して制御します。

1. ディスパッチャはメモリ上に「エンドポイントごとの最終実行時刻（`last_run_time[endpoint]`）」を記録する辞書を持つ。
2. ループ内で、現在 `pending` のジョブが存在する**ユニークなエンドポイントの一覧**を取得する。
3. **各エンドポイントに対して以下を評価する:**
   * 設定ファイルから、そのエンドポイントの `max_concurrent_jobs` と `min_interval_seconds` を決定する。（`endpoint_rate_limits` に指定がなければ `default_rate_limit` を使用）
   * 現在そのエンドポイント向けに実行中（`running` または `polling`）のジョブ数をカウントする。
   * 実行中の数が `max_concurrent_jobs` 以上なら、空きが出るまでこのエンドポイントの `pending` ジョブはスキップする。
   * 現在時刻と `last_run_time[endpoint]` の差分が `min_interval_seconds` 未満なら、インターバルを満たすまでこのエンドポイントのジョブはスキップする。
4. 条件をクリアしたエンドポイントについて、最も古い `pending` ジョブを1つ取得して `running` に更新し、`ThreadPoolExecutor` に submit する。同時に `last_run_time[endpoint]` を現在時刻に更新する。
5. 短いスリープ（例: `time.sleep(0.1)`）を挟んでループを繰り返す。

### 4.5. ジョブ実行フロー（ワーカー内部）

各ジョブはスレッドプール内のスレッドで以下を実行します。

1. `MCPAsyncClient` を生成し、`initialize()` でセッション確立。セッションIDをDBに保存。
2. `submit()` で外部MCPサーバーにジョブ投入。返却された `remote_job_id` をDBに保存。ステータスを `polling` に更新。
3. `check_status()` を繰り返し、外部ジョブの完了を待つ。
4. 完了したら `get_result()` で結果を取得。結果（リモートURL等）をDBに保存。ステータスを `completed` に更新。
5. 外部MCPサーバーがエラーを返した場合、エラー情報をDBに保存しステータスを `failed` に更新。自動リトライは行わない。

### 4.6. シングルトンと自動起動・終了

* **シングルトン**: `http.server` 起動時の `socket.bind()` でポートが使用中の場合 `OSError` となるため、別のワーカーが生きていると判断して安全に終了します。

* **自動起動 (Client側)**:
  OS非依存となるよう、実行環境に合わせてプロセスの切り離し方を分岐させます。
  ```python
  import subprocess
  import sys

  kwargs = {}
  if sys.platform == "win32":
      # Windowsの場合：コンソールを出さず、プロセスを完全に切り離す
      kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
  else:
      # macOS / Linuxの場合：新しいセッションを作成し、親プロセスから切り離す
      kwargs["start_new_session"] = True

  subprocess.Popen([sys.executable, "mcp_worker_daemon.py"], **kwargs)
  ```

* **自動終了 (Worker側)**:
  ディスパッチャループ内で「稼働中の全ジョブ数」と「最終APIアクセス時刻」を監視し、ジョブが0でかつ設定されたアイドル時間を超過した場合、サーバーをシャットダウンします。

### 4.7. エラーハンドリング

> **⚠️ 堅牢化による変更**: 以下は初期設計です。堅牢化により、ConnectionError/503/504/429 に対する指数バックオフリトライが追加されました。詳細は [queue_system_hardening.md](queue_system_hardening.md) を参照してください。

* 外部MCPサーバーがエラー（429、500等）を返した場合、**自動リトライは行わず**、エラー情報をDBの `error` カラムに保存してステータスを `failed` に更新します。
* クライアントは `--wait` で `failed` ステータスとエラー詳細を受け取り、ログに出力します。
* リトライが必要な場合は、AIエージェント（またはユーザー）が改めてジョブを投入します。

### 4.8. ダウンロード処理

> **⚠️ 堅牢化による変更**: 以下は初期設計です。堅牢化により、ワーカー側でのファイルダウンロード機能が追加されました。詳細は [queue_system_hardening.md](queue_system_hardening.md) を参照してください。

* ワーカーはファイルダウンロードを行いません。外部MCPサーバーが返すリモートURL（fal.ai等）を結果としてDBに保存するのみです。
* ファイルダウンロードは**クライアント側**で行います。これにより:
  - クライアント（およびCLI）がURLを直接把握できる。
  - ダウンロード先パスをクライアント側で柔軟に制御できる。
  - ワーカーの責務をキューイングに限定できる。

## 5. 実装計画（テスト駆動開発: TDD）

本プロジェクトはコンポーネントが多いため、依存関係の少ない内側からTDDで実装を進めます。

* **Step 1: データベース・モデル層 (db.py)**
  * インメモリDB (`:memory:`) を使用し、ジョブのCRUD操作とステータス更新をテスト。
  * セッションID・リモートジョブIDの保存と取得をテスト。

* **Step 2: キューディスパッチャとレートリミット (dispatcher.py)**
  * ダミーのHTTPリクエスト関数（モック）を注入。
  * **(重要)** 異なる2つのエンドポイント（AとB）宛のジョブを投入し、Aには並行数1/間隔長め、Bには並行数5/間隔短めの設定を与え、それぞれの制限に沿って独立して処理されるかをテスト。

* **Step 3: Worker HTTP層 (mcp_worker_daemon.py)**
  * テスト用ポートでサーバーを起動し、API経由でのジョブ登録と `GET /api/jobs/{id}` による状態取得の正常性をテスト。
  * アイドルタイムアウトで自動終了するかテスト。

* **Step 4: フロントクライアント (mcp_async_call.py 改修)**
  * `--submit-only`, `--wait`, `--blocking` の3モードが正しく動作するかテスト。
  * Workerが落ちている状態でリクエストを開始し、自動起動ロジックが各OSで正常に走るかテスト。
