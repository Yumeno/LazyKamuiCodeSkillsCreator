# キューシステム堅牢化設計書

## 1. 目的

`docs/queue_system_design.md` に基づいて実装されたキューイングシステムは、AIエージェントと外部MCPサーバー間のレートリミット制御を実現しています。

しかし、運用レビューにおいて以下の課題が明らかになりました。

1. **ゾンビジョブの発生**: ワーカープロセスが強制終了（SIGKILL、PC再起動等）された場合、DB上で `running` や `polling` のまま残るジョブが発生し、以後更新されない
2. **リトライロジックの欠如**: 一時的なネットワークエラーや外部MCPサーバーのコールドスタート（503/504）に対して即座に失敗とする設計のため、成功率が低下する
3. **HTTP 429 への動的対応不足**: 固定値 `min_interval_seconds` による制御のみで、外部MCPサーバーが `Retry-After` ヘッダー付きの 429 を返した場合に動的に対応できない
4. **成果物の消失リスク**: ファイルのダウンロード処理がクライアント側にのみ存在するため、クライアント（AIエージェントのCLI等）が異常終了した場合、外部MCPサーバーで生成された成果物が取得不能となり、ユーザーが支払ったAPIコストが無駄になる

さらに、設計上の問題として以下が判明しました。

5. **DB配置の脆弱性**: `jobs.db` がスキルディレクトリ内に作成され、`generate_skill.py` による再生成時に `queue_config.json` のユーザー設定が上書き消失する可能性がある
6. **複数スキル共有時の設計不備**: ワーカーはポート54321で共有されるが、DBの配置先が起動順序に依存して不定となり、ジョブ管理が一貫しない

本設計書の目的は、これら6つの課題を解決し、**ユーザーのAPIコストに対して誠実で、障害耐性の高いキューシステム**を実現することです。

## 2. 要件定義

### 2.1. 機能要件

1. **起動時ゾンビジョブ回復**: ワーカー起動時に、前回のプロセスが残した未完了ジョブを検出し、状態に応じて「回復」または「失敗終了」を行うこと。`running`（submit未完了）は回復不能のため `failed` とし、`polling`（submit済み、`remote_job_id` あり）は外部MCPサーバーへのステータス確認と結果取得を試みること。
2. **指数バックオフリトライ**: 外部MCPサーバーへの HTTP 通信（submit、result取得）で一時的エラー（`ConnectionError`、`503`、`504`、`429`）が発生した場合、最大3回の指数バックオフ（2秒→4秒→8秒）でリトライすること。429の場合は `Retry-After` ヘッダー値を優先すること。
3. **429エンドポイント一時停止**: 外部MCPサーバーから `429 Too Many Requests` を受信した場合、そのエンドポイントへの新規ジョブディスパッチを `Retry-After` 秒間一時停止し、他のエンドポイントへのディスパッチには影響を与えないこと。
4. **ワーカーサイドダウンロード**: ジョブ完了時、ワーカーが結果JSONの取得だけでなく、結果に含まれるURLからのファイルダウンロードまで行い、指定されたローカルディレクトリに保存すること。クライアントが喪失してもダウンロード済みファイルが残ること。
5. **共有DB・結果ディレクトリの一元管理**: 全スキルで共有するDB（`jobs.db`）と結果ディレクトリ（`results/`）を、スキルディレクトリの外（`{project_root}/.claude/queue/`）に配置し、スキルの再生成や削除の影響を受けないこと。
6. **エンドポイント別 rate limit の動的登録**: 複数のスキルが同一ワーカーを共有する際、各スキルがジョブ投入時にそのエンドポイントの rate limit 情報を送信し、ワーカーがインメモリで動的に登録すること。

### 2.2. 非機能要件

1. **後方互換性**: 全ての新規引数・メソッドにデフォルト値を付与し、既存テスト（107件）が修正なしで通過すること。DBスキーマの変更を行わないこと。
2. **標準ライブラリ優先**: 既存の `requests` ライブラリ依存以外の外部パッケージを追加しないこと。
3. **クロスプラットフォーム対応**: Windows、macOS、Linux でパスの解決やプロセスの起動が同一の挙動を保証すること。
4. **TDD開発**: テストを先に書き、テストが通ることを確認してから実装を進めること。
5. **設定保護**: `generate_skill.py` による再生成時に、ユーザーがカスタマイズした `queue_config.json` の設定値（rate limit等）を保持すること。

## 3. 基本設計（アーキテクチャ）

### 3.1. ディレクトリ構造の変更

```
変更前（現行設計）:
  {skill_dir}/
    queue_config.json       ← スキルごとに生成、再生成で上書きリスク
    jobs.db                 ← queue_config.json と同階層に生成、起動順依存
    scripts/
      job_queue/
      mcp_worker_daemon.py

変更後:
  {project_root}/.claude/
    queue/
      queue_config.json     ← 共通設定（ワーカー起動パラメータ）
      jobs.db               ← 全スキル共有
      results/              ← ワーカーがダウンロードした成果物
        {job_id}/
          result.json       ← MCP応答の生JSON + ローカルファイルパス
          output.png         ← ダウンロード済みファイル（複数可）

  {skill_dir}/
    queue_config.json       ← エンドポイント個別 rate limit のみ（保護対象）
    scripts/
      job_queue/            ← コード（再生成で上書きOK）
      mcp_worker_daemon.py
```

### 3.2. 設定の二層構造

設定を「共通設定」と「スキル個別設定」に分離します。

**共通設定** (`.claude/queue/queue_config.json`):

```json
{
  "host": "127.0.0.1",
  "port": 54321,
  "idle_timeout_seconds": 60,
  "default_rate_limit": {
    "max_concurrent_jobs": 2,
    "min_interval_seconds": 2.0
  },
  "results_dir": null,
  "job_retention_seconds": 86400
}
```

ワーカーの起動パラメータとデフォルト値を定義します。`results_dir` が `null` の場合は `{project_root}/.claude/queue/results` を使用します。`job_retention_seconds` は古いジョブの自動削除までの保持期間です。

**スキル個別設定** (`{skill_dir}/queue_config.json`):

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
    "http://fal.ai:8000": {
      "max_concurrent_jobs": 1,
      "min_interval_seconds": 10.0
    }
  }
}
```

従来のフォーマットを維持します。クライアントがこのファイルを読み、ジョブ投入時（`POST /api/jobs`）に `endpoint_rate_limits` を body に含めて送信します。

### 3.3. Rate Limit 動的登録フロー

```
[スキルA wrapper]                    [スキルB wrapper]
  │ skill-A/queue_config.json 読込     │ skill-B/queue_config.json 読込
  │                                    │
  │ POST /api/jobs                     │ POST /api/jobs
  │ { endpoint: "http://fal.ai",       │ { endpoint: "http://veo.api",
  │   rate_limits: {                   │   rate_limits: {
  │     max_concurrent: 1,             │     max_concurrent: 3,
  │     min_interval: 10.0 },          │     min_interval: 1.0 },
  │   ... }                           │   ... }
  │                                    │
  └──────────────┬─────────────────────┘
                 │
                 v
  [ワーカー (port 54321, 全スキル共有)]
    Dispatcher._endpoint_limits (インメモリ):
      "http://fal.ai"  → (1, 10.0)    ← スキルAから受信・登録
      "http://veo.api" → (3, 1.0)     ← スキルBから受信・登録

    jobs.db: 全スキルのジョブを一元管理
    results/: 全スキルの成果物を一元保存
```

**設計ポイント**:

- rate limit はインメモリのみ管理し、共通configファイルには書き戻さない
- ワーカー再起動後もクライアントが次回 `POST /api/jobs` で再送するため問題なし
- 共通configの `default_rate_limit` で事前に設定されたエンドポイントは上書きしない（共通config優先）
- `start_worker()` は共通config (`.claude/queue/queue_config.json`) のパスを `--config` に渡す

### 3.4. ワーカーサイドダウンロード

```
変更前:
  [外部MCP] ─結果JSON(URL)─→ [ワーカー] ─DB保存─→ [クライアント] ─ファイルDL
                               URLのみ保存            ここでDL
                                                      ↑ 死亡→成果物消失

変更後:
  [外部MCP] ─結果JSON(URL)─→ [ワーカー] ─DL+DB保存─→ [クライアント]
                               URLからDL              ローカルパスを受取
                               results/{job_id}/ に保存 DL不要
                                                      ↑ 死亡しても成果物は残存
```

### 3.5. 起動時処理フロー

```
ワーカー起動
  │
  ├── 1. 共通config読込 (.claude/queue/queue_config.json)
  │     存在しなければデフォルト値で自動生成
  │
  ├── 2. DB・結果ディレクトリ初期化 (.claude/queue/)
  │
  ├── 3. 古い completed/failed ジョブを削除（パージ）
  │     retention_seconds（デフォルト24時間）超過分を DELETE
  │     対応する results/{job_id}/ ディレクトリも削除
  │
  ├── 4. ゾンビジョブの状態遷移
  │     ├── running (remote_job_id なし) → failed（submit未完了、回復不能）
  │     └── polling (remote_job_id あり) → recovering（回復専用ステータス）
  │           session_id, remote_job_id はDB上に保持したまま
  │
  ├── 5. HTTP サーバー起動
  ├── 6. Dispatcher 起動
  │     recovering ジョブを rate limit 適用の上でディスパッチ
  │     submit をスキップし polling → completed/failed と遷移
  └── 7. Idle Monitor 起動
```

### 3.6. ゾンビ回復と Rate Limit の整合性

回復ジョブも Dispatcher を経由して実行するため、通常のジョブと同じ rate limit が適用されます。submit の再実行（二重課金）を避けるため、回復専用ステータス `recovering` を導入します。

**ステータス遷移図（拡張版）**:

```
新規ジョブ:
  pending → running → polling → completed / failed

回復ジョブ:
  polling（ゾンビ） → recovering → polling → completed / failed
                       ↑                ↑
                   起動時に変更     Dispatcher がディスパッチ
                                   running を経由しない
                                   = submit しない = 二重課金なし
```

**Dispatcher の動作**:

```
polling ゾンビ 5件が起動時に recovering に変更された場合:

  Dispatcher.dispatch_once():
    1. 通常の pending ジョブのディスパッチ（既存ロジック）
    2. recovering ジョブのディスパッチ（追加ロジック）
       endpoint "http://fal.ai" の rate limit: max_concurrent=1, min_interval=10.0s
       → recovering ジョブを1件取得
       → status を "polling" に更新（running をスキップ）
       → スレッドプールに submit して _run_job 実行
       → rate limit に従い、次の recovering は min_interval 後

  execute_job(job):
    job["remote_job_id"] が存在する（DB上に保持されている）
    → submit フェーズをスキップ
    → _poll_and_get_result() から再開（poll + result + download）
```

**count_active_jobs() への影響**:

`recovering` は外部通信をまだ開始していない状態のため、active（`running` + `polling`）には含めません。Dispatcher がディスパッチして `polling` にした時点で active カウントに入ります。

**execute_job 内の分岐**:

```python
def execute_job(job):
    if job.get("remote_job_id"):
        # 回復ジョブ: submit 済み → poll+result フェーズから再開
        client = MCPAsyncClient(endpoint, headers)
        client.session_id = job["session_id"]
        _poll_and_get_result(store, job["id"], client, job["remote_job_id"], ...)
    else:
        # 新規ジョブ: submit → poll+result
        request_id = _with_retry(lambda: client.submit(submit_tool, args))
        store.update_status(job["id"], "polling", session_id=..., remote_job_id=...)
        _poll_and_get_result(store, job["id"], client, request_id, ...)
```

## 4. 詳細設計

### 4.1. DB・結果ディレクトリの再配置

#### 4.1.1. プロジェクトルート解決

`mcp_worker_daemon.py` に `find_project_root()` 関数を追加します。

```python
def find_project_root(start_path: str) -> str:
    """start_path から上位ディレクトリを辿り、.claude/ を持つプロジェクトルートを返す。
    見つからなければ start_path の親ディレクトリを返す。"""
    current = os.path.dirname(os.path.abspath(start_path))
    while True:
        if os.path.isdir(os.path.join(current, ".claude")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.dirname(os.path.abspath(start_path))
        current = parent
```

#### 4.1.2. DBパス解決の変更

`main()` 内の DB パス決定ロジックを変更します。

```python
# 変更前:
if args.config:
    db_dir = os.path.dirname(os.path.abspath(args.config))
else:
    db_dir = os.getcwd()
db_path = os.path.join(db_dir, "jobs.db")

# 変更後:
project_root = find_project_root(args.config or os.getcwd())
queue_dir = os.path.join(project_root, ".claude", "queue")
os.makedirs(queue_dir, exist_ok=True)
db_path = os.path.join(queue_dir, "jobs.db")
results_dir = config_dict.get("results_dir") or os.path.join(queue_dir, "results")
os.makedirs(results_dir, exist_ok=True)
```

#### 4.1.3. .gitignore

プロジェクトルートの `.gitignore` に以下を追加します。

```
# MCP Queue runtime data
.claude/queue/
```

### 4.2. ワーカーサイドダウンロード

#### 4.2.1. execute_job の分割

現在の `execute_job` クロージャを2つのフェーズに分割します。

```
execute_job(job):
  ├── Submit フェーズ: client.submit() → update "polling"
  └── _poll_and_get_result():  ← ゾンビ回復と共用
        ├── Polling フェーズ: client.check_status() ループ
        ├── Result フェーズ: client.get_result()
        ├── Download フェーズ: extract_download_urls() → download_file()
        │     保存先: {results_dir}/{job_id}/
        └── DB更新: update "completed" (result に local_files 含む)
```

#### 4.2.2. ダウンロード処理

`mcp_async_call.py` の既存関数 `extract_download_urls()` と `download_file()` を import して使用します。

```python
def _download_results(result_resp: dict, job_id: str, results_dir: str) -> dict:
    """result_resp 内の URL をダウンロードして results_dir/{job_id}/ に保存する。

    戻り値:
      {
        "remote_result": <元のresult_resp>,
        "local_files": ["/abs/path/file1.png", ...],
        "download_errors": ["error msg", ...]
      }
    """
```

- ダウンロード失敗は非致命的: `download_errors` にエラーメッセージを記録し、ジョブ自体は `completed` とする
- `result.json` を `{results_dir}/{job_id}/result.json` に保存（DB の `result` カラムにも同内容を格納）

#### 4.2.3. クライアント側の変更

`mcp_async_call.py` の `_queue_blocking()` で、戻り値の `result` JSON に `local_files` が含まれていればクライアント側ダウンロードをスキップします。

```python
# _queue_blocking() 内
result_data = json.loads(result["result"])
if "local_files" in result_data:
    # ワーカー側でDL済み → クライアント側DLをスキップ
    result["saved_paths"] = result_data["local_files"]
```

### 4.3. 起動時ゾンビジョブ回復

#### 4.3.1. JobStore への新規メソッド追加

`job_queue/db.py` に以下を追加します。

```python
def get_stale_jobs(self, statuses: list[str]) -> list[dict]:
    """指定ステータスのジョブを全て返す（ゾンビ回復用）。"""
    placeholders = ",".join("?" * len(statuses))
    cur = self.conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
        statuses,
    )
    return [dict(row) for row in cur.fetchall()]

def purge_old_jobs(self, retention_seconds: float = 86400.0) -> int:
    """completed/failed の古いジョブを削除する。戻り値: 削除件数。"""
    cur = self.conn.execute(
        """DELETE FROM jobs
           WHERE status IN ('completed', 'failed')
             AND (julianday('now') - julianday(updated_at)) * 86400.0 > ?""",
        (retention_seconds,),
    )
    self.conn.commit()
    return cur.rowcount
```

#### 4.3.2. WorkerApp.start() の拡張

`job_queue/worker.py` の `start()` メソッドにパージ・ゾンビ処理を追加します。

```python
def start(self, rollback_fn=None, retention_seconds: float = 86400.0):
    """起動時にパージ→ゾンビ状態遷移→通常起動の順で実行する。"""
    # 1. 古いジョブのパージ
    purged = self.store.purge_old_jobs(retention_seconds)

    # 2. ゾンビジョブの状態遷移（recovering or failed）
    if rollback_fn is not None:
        rollback_fn(self.store)

    # 3. 通常起動（既存コード）
    self._running = True
    # ... → Dispatcher が recovering ジョブを rate limit 適用の上でディスパッチ
```

#### 4.3.3. ゾンビ状態遷移ロジック

`mcp_worker_daemon.py` にゾンビジョブの状態遷移関数を追加します。

```python
def create_rollback_fn():
    """ゾンビジョブ状態遷移関数を作成する。"""

    def rollback_stale_jobs(store):
        stale = store.get_stale_jobs(["running", "polling"])
        for job in stale:
            if job["status"] == "running" or not job.get("remote_job_id"):
                # submit未完了 → 回復不能、failed に
                store.update_status(job["id"], "failed",
                    error="Worker restarted before submit completed")
            else:
                # polling → recovering に変更
                # session_id, remote_job_id はDB上に保持したまま
                store.update_status(job["id"], "recovering")

    return rollback_stale_jobs
```

#### 4.3.4. Dispatcher の recovering ジョブ対応

`job_queue/dispatcher.py` に recovering ジョブのディスパッチロジックを追加します。

```python
def dispatch_once(self) -> int:
    dispatched = 0

    # 1. 通常の pending ジョブのディスパッチ（既存ロジック）
    endpoints = self.store.get_pending_endpoints()
    for ep in endpoints:
        # ... 既存の rate limit チェックとディスパッチ ...

    # 2. recovering ジョブのディスパッチ（追加）
    recovering_endpoints = self.store.get_recovering_endpoints()
    for ep in recovering_endpoints:
        max_concurrent, min_interval = self.config.get_limits(ep)

        # 同じ rate limit チェック
        if time.monotonic() < self._pause_until.get(ep, 0.0):
            continue
        active = self.store.count_active_jobs(ep)
        if active >= max_concurrent:
            continue
        last_run = self._last_run_time.get(ep, 0.0)
        if (time.monotonic() - last_run) < min_interval:
            continue

        job = self.store.get_oldest_recovering(ep)
        if job is None:
            continue

        # running をスキップし、直接 polling に（submit は行わない）
        self.store.update_status(job["id"], "polling")
        self._last_run_time[ep] = time.monotonic()
        self._pool.submit(self._run_job, job)
        dispatched += 1

    return dispatched
```

`job_queue/db.py` に以下のメソッドを追加します。

```python
def get_recovering_endpoints(self) -> list[str]:
    """recovering ジョブが存在するエンドポイントのリストを返す。"""
    cur = self.conn.execute(
        "SELECT DISTINCT endpoint FROM jobs WHERE status = 'recovering'"
    )
    return [row[0] for row in cur.fetchall()]

def get_oldest_recovering(self, endpoint: str) -> dict | None:
    """指定エンドポイントの最も古い recovering ジョブを返す。"""
    cur = self.conn.execute(
        "SELECT * FROM jobs WHERE status = 'recovering' AND endpoint = ? ORDER BY created_at ASC LIMIT 1",
        (endpoint,),
    )
    row = cur.fetchone()
    return dict(row) if row else None
```

この設計により：
- 回復ジョブも `max_concurrent_jobs` と `min_interval_seconds` の制約下で実行される
- `running` ステータスを経由しない = submit は再実行されない = 二重課金なし
- `recovering` は active カウント（`running` + `polling`）に含まれない → ディスパッチ時に `polling` にした時点でカウントに入る
- ステータス遷移が明確: `polling`(ゾンビ) → `recovering` → `polling` → `completed`/`failed`

### 4.4. 指数バックオフリトライ

#### 4.4.1. リトライヘルパー関数

`mcp_worker_daemon.py` に以下を追加します。

```python
def _is_retryable(exc: Exception) -> bool:
    """リトライ対象のエラーか判定する。"""
    import requests
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (429, 503, 504)
    return False

def _get_retry_after(exc: Exception) -> float | None:
    """429の Retry-After ヘッダー値を取得する（上限60秒）。"""
    import requests
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code == 429:
            val = exc.response.headers.get("Retry-After")
            if val:
                try:
                    return min(float(val), 60.0)
                except ValueError:
                    pass
    return None

def _with_retry(fn, max_retries: int = 3):
    """指数バックオフリトライラッパー。2s→4s→8s。429時はRetry-After優先。"""
    backoff = [2.0, 4.0, 8.0]
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if not _is_retryable(e) or attempt >= max_retries:
                raise
            wait = _get_retry_after(e) or backoff[min(attempt, len(backoff) - 1)]
            time.sleep(wait)
```

#### 4.4.2. 適用箇所

- `client.submit()` の呼び出し: `_with_retry(lambda: client.submit(submit_tool, args))`
- `client.get_result()` の呼び出し: `_with_retry(lambda: client.get_result(result_tool, request_id))`
- polling ループ内: 個別の `check_status()` エラーは skip して次のサイクルへ（ループ自体が自然なリトライ機構）

#### 4.4.3. Dispatcher エンドポイント一時停止

`job_queue/dispatcher.py` に以下を追加します。

```python
class Dispatcher:
    def __init__(self, ...):
        # ... 既存フィールド ...
        self._pause_until: dict[str, float] = {}

    def pause_endpoint(self, endpoint: str, seconds: float):
        """エンドポイントへの新規ディスパッチを seconds 秒間停止する。"""
        self._pause_until[endpoint] = time.monotonic() + seconds

    def dispatch_once(self) -> int:
        # ... 既存ロジック ...
        for ep in endpoints:
            # ポーズチェック（追加）
            if time.monotonic() < self._pause_until.get(ep, 0.0):
                continue
            # ... 既存の並行数・インターバルチェック ...
```

429 を受信した `execute_job` 内で `dispatcher.pause_endpoint(ep, retry_after)` を呼び出し、そのエンドポイントの新規ジョブディスパッチを一時停止します。

### 4.5. エンドポイント別 rate limit の動的登録

#### 4.5.1. Dispatcher への動的登録メソッド

```python
class Dispatcher:
    def register_endpoint_limits(self, endpoint: str, max_concurrent: int, min_interval: float):
        """エンドポイントの rate limit をインメモリ登録する。既登録なら上書きしない。"""
        if endpoint not in self.config._endpoint_limits:
            self.config._endpoint_limits[endpoint] = (max_concurrent, min_interval)
```

#### 4.5.2. POST /api/jobs の拡張

`worker.py` の `do_POST` ハンドラで `rate_limits` フィールドを処理します。

```python
# POST /api/jobs の body に追加される optional フィールド:
# "rate_limits": { "max_concurrent_jobs": 1, "min_interval_seconds": 10.0 }

rate_limits = body.get("rate_limits")
if rate_limits and endpoint:
    app.dispatcher.register_endpoint_limits(
        endpoint,
        rate_limits.get("max_concurrent_jobs", 2),
        rate_limits.get("min_interval_seconds", 2.0),
    )
```

#### 4.5.3. クライアント側の rate limit 送信

`mcp_async_call.py` のキューモード関数で、スキルの `queue_config.json` から `endpoint_rate_limits` を読み、`POST /api/jobs` の body に含めます。

```python
# submit_job() 呼び出し前にスキルconfig読込
if queue_config_path:
    with open(queue_config_path) as f:
        skill_config = json.load(f)
    ep_limits = skill_config.get("endpoint_rate_limits", {}).get(endpoint)
    if ep_limits:
        payload["rate_limits"] = ep_limits
```

#### 4.5.4. start_worker() の共通config化

`job_queue/client.py` の `start_worker()` が渡す `--config` を共通config に変更します。

```python
def start_worker(worker_script, config_path=None, ...):
    # config_path からプロジェクトルートを探し、共通configパスを使用
    common_config = find_common_config(config_path)
    cmd = [sys.executable, worker_script]
    if common_config:
        cmd.extend(["--config", common_config])
```

### 4.6. generate_skill.py の保護

#### 4.6.1. queue_config.json のマージ戦略

`_copy_queue_files()` の `queue_config.json` 書き込みロジックを変更します。

```python
config_path = skill_dir / "queue_config.json"
new_config = generate_queue_config(endpoint)

if config_path.exists():
    # 既存ファイルが存在 → ユーザー設定を保持しつつマージ
    existing = json.loads(config_path.read_text(encoding="utf-8"))
    merged = {**new_config}
    # ユーザーが変更しうるフィールドは既存から保持
    for key in ("idle_timeout_seconds", "default_rate_limit", "endpoint_rate_limits",
                "results_dir", "job_retention_seconds"):
        if key in existing:
            merged[key] = existing[key]
    # 新エンドポイントの追加（既存エンドポイントは保持）
    if endpoint and endpoint not in merged.get("endpoint_rate_limits", {}):
        merged.setdefault("endpoint_rate_limits", {})[endpoint] = \
            new_config.get("endpoint_rate_limits", {}).get(endpoint, {})
    config_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
else:
    # 新規作成
    config_path.write_text(json.dumps(new_config, indent=2, ensure_ascii=False), encoding="utf-8")
```

## 5. 実装計画（テスト駆動開発: TDD）

全ステップにおいて、テストを先に書き、テストが失敗することを確認してから実装を行います。

### Step 1: DB層の拡張 (`job_queue/db.py`)

**テスト対象**: `get_stale_jobs()`, `purge_old_jobs()`

```
テスト:
  - running/polling ジョブが get_stale_jobs で取得できること
  - pending/completed は取得されないこと
  - retention=0 で completed/failed が即削除されること
  - retention が十分大きい場合は削除されないこと
  - 空DBでエラーにならないこと

実装:
  - db.py に 2メソッド追加
```

### Step 2: Dispatcher の拡張 (`job_queue/dispatcher.py`)

**テスト対象**: `pause_endpoint()`, `register_endpoint_limits()`, `dispatch_once()` のポーズチェック

```
テスト:
  - pause_endpoint 後に dispatch_once がそのエンドポイントをスキップすること
  - ポーズ期限切れ後にディスパッチが再開すること
  - 他のエンドポイントはポーズの影響を受けないこと
  - register_endpoint_limits で新エンドポイントが登録されること
  - 既登録エンドポイントは上書きされないこと

実装:
  - dispatcher.py に _pause_until, pause_endpoint(), register_endpoint_limits() 追加
  - dispatch_once() にポーズチェック追加
  - QueueConfig に results_dir フィールド追加
```

### Step 3: Worker の拡張 (`job_queue/worker.py`)

**テスト対象**: `WorkerApp` の `results_dir` 引数、`start()` の回復・パージ呼び出し、`POST /api/jobs` の `rate_limits` 処理

```
テスト:
  - WorkerApp(results_dir=...) で results_dir が保持されること
  - start(recover_fn=...) で recover_fn が呼ばれること
  - start(retention_seconds=0) で purge が実行されること
  - POST /api/jobs に rate_limits を含めると register_endpoint_limits が呼ばれること

実装:
  - worker.py に results_dir 引数追加、start() 拡張、do_POST の rate_limits 処理追加
```

### Step 4: リトライロジック (`mcp_worker_daemon.py`)

**テスト対象**: `_is_retryable()`, `_get_retry_after()`, `_with_retry()`

```
テスト:
  - ConnectionError がリトライ対象と判定されること
  - HTTPError 503/504 がリトライ対象と判定されること
  - HTTPError 429 がリトライ対象で、Retry-After が取得されること
  - HTTPError 400/404 がリトライ対象外であること
  - _with_retry が最大3回リトライすること
  - 成功時はリトライせず即座に返ること
  - リトライ不可エラーで即座に raise すること

実装:
  - mcp_worker_daemon.py に 3ヘルパー関数追加
```

### Step 5: execute_job の分割とダウンロード (`mcp_worker_daemon.py`)

**テスト対象**: `_poll_and_get_result()`, `_download_results()`, 分割後の `execute_job()`

```
テスト:
  - _poll_and_get_result が完了ジョブの結果を取得し completed に更新すること
  - _poll_and_get_result が失敗ジョブを failed に更新すること
  - _download_results が URL からファイルをダウンロードし local_files を返すこと
  - ダウンロード失敗時に download_errors に記録され、ジョブは completed のままであること
  - execute_job が submit → poll → result → download の一連のフローを実行すること

実装:
  - execute_job の分割、_poll_and_get_result 抽出、_download_results 追加
```

### Step 6: ゾンビ回復 — recovering ステータスと Dispatcher 対応

**テスト対象**: `create_rollback_fn()`, Dispatcher の recovering ディスパッチ, `execute_job` の `remote_job_id` 分岐

```
テスト:
  - running ジョブ（remote_job_id なし）が failed に更新されること
  - polling ジョブ（remote_job_id あり）が recovering に更新されること
  - recovering ジョブの session_id, remote_job_id が保持されていること
  - Dispatcher が recovering ジョブを rate limit 適用の上でディスパッチすること
  - Dispatcher が recovering ジョブを polling に更新すること（running をスキップ）
  - count_active_jobs が recovering を含まないこと
  - execute_job に remote_job_id 付きジョブが渡された場合、submit がスキップされること
  - execute_job に remote_job_id 付きジョブが渡された場合、poll+result から再開されること

実装:
  - db.py に get_recovering_endpoints(), get_oldest_recovering() 追加
  - dispatcher.py の dispatch_once() に recovering ディスパッチ追加
  - create_rollback_fn() 追加
  - execute_job の先頭に remote_job_id 存在チェック分岐を追加
  - main() に組み込み
```

### Step 7: クライアント・設定統合

**テスト対象**: `start_worker()` の共通config化、`submit_job()` の rate_limits 送信、`_queue_blocking()` の local_files 対応

```
テスト:
  - start_worker が共通configパスを使用すること
  - submit_job が rate_limits を body に含めること
  - _queue_blocking で local_files がある場合にクライアント側DLをスキップすること

実装:
  - client.py の start_worker 変更
  - mcp_async_call.py の rate_limits 送信、local_files 対応
```

### Step 8: generate_skill.py の保護

**テスト対象**: `_copy_queue_files()` のマージ戦略

```
テスト:
  - 既存の queue_config.json のカスタム rate limit が再生成後も保持されること
  - 新規エンドポイントが既存設定を消さずに追加されること
  - queue_config.json が存在しない場合は新規作成されること
  - 壊れた JSON ファイルがある場合は新規作成にフォールバックすること

実装:
  - _copy_queue_files() のマージロジック実装
```
