# **MCP リクエスト・キューイングシステム設計書**

## **1\. 目的**

現在の LazyKamuiCodeSkillsCreator は、AIエージェント（Claude Code等）から呼び出された際、直接対象のMCPサーバーに対して非同期ジョブ（Submit → Statusポーリング → Result）を実行します。

しかし、AIが複数ツールを並列かつ高速に呼び出した場合、サーバーへの同時アクセスが急増し、過剰な負荷やレートリミット（429 Too Many Requests）エラーを引き起こす問題があります。

本プロジェクトの目的は、**クライアントと外部MCPサーバーの間にローカルのキューイング層（ワーカープロセス）を設け、リクエストの並行数と実行間隔を制御すること**です。

さらに、複数の異なるMCPサーバーを利用する場合でも、\*\*「宛先（エンドポイント）ごとに独立したキューと、個別の制限値」\*\*を持たせることで、重い処理を行うサーバーに引きずられて他の軽量なサーバーが待たされること（Head-of-Lineブロッキング）を防ぎます。

同時に、AIエージェントの利用体験を損なわないよう「ワーカーの完全な自動起動・自動終了」を実現し、ユーザーがデーモンプロセスを意識せずに済むアーキテクチャを構築します。

## **2\. 要件定義**

### **2.1. 機能要件**

1. **エンドポイントごとのレートリミットと並行数制御**: 外部MCPサーバーへの負荷を防ぐため、「同時実行数の上限」および「ジョブ実行の最低間隔」を設定します。これらはデフォルト値を持つだけでなく、**エンドポイント（URL）ごとに個別の値を設定**して柔軟に制限を制御できること。  
2. **ロングポーリングによる非同期完了通知**: クライアントはDBへ何度もポーリングするのではなく、完了を待機するAPIエンドポイントを叩き、結果が出た時点で即座に応答を受け取ること。  
3. **オンデマンド自動起動**: ワーカープロセスが起動していない場合、クライアントは接続エラーを検知し、自動的にバックグラウンドでワーカーを起動してからリクエストを再送すること。  
4. **自動終了（アイドルタイムアウト）**: ワーカーはキューが空になり、一定時間（例: 60秒）新たなリクエストがない場合、自動的に終了してリソースを解放すること。  
5. **ポートの可変性**: ワーカーが使用するHTTPポートは固定せず、設定ファイル（または環境変数）から変更可能であること。  
6. **ジョブの永続化**: 処理状況と結果をSQLiteに保存し、ワーカークラッシュ時でも状態を保護すること。

### **2.2. 非機能要件**

1. **標準ライブラリの利用**: 原則としてPython標準ライブラリ（sqlite3, http.server, threading, subprocess 等）のみを使用し、外部依存を増やさないこと。  
2. **OS非依存（クロスプラットフォーム対応）**: Windows、macOS、Linuxのいずれの環境でも同一の挙動を保証すること。特にバックグラウンドプロセス起動時において、OSに応じた適切な分離処理（黒窓回避、セッション切り離し等）を行うこと。  
3. **データベースのロック回避**: SQLiteの database is locked エラーを防ぐため、ロングポーリング中はDBへの読み書きを行わず、メモリ上のEventオブジェクトで待機すること。

## **3\. 基本設計（アーキテクチャ）**

**「ローカルHTTP API ＋ SQLite ＋ インメモリEvent（ロングポーリング）」** のハイブリッド方式を採用します。ワーカープロセス自体はシステム全体で1つ（シングルトン）ですが、内部のディスパッチャがエンドポイントごとに設定された制限値を読み取り、賢くタスクを振り分けます。

### **3.1. コンポーネント構成**

1. **Client (mcp\_async\_call.py 等)**  
   * 従来のMCP直接通信をやめ、ローカルの Worker に対してHTTPリクエストを送信します。  
   * ConnectionError 発生時は Worker をバックグラウンド起動し、ヘルスチェックが通るまで待機します。  
   * ジョブ登録後、/wait エンドポイントを叩いて結果を待ちます。  
2. **Worker (mcp\_worker\_daemon.py)**  
   * ローカル（127.0.0.1）で待ち受ける HTTP サーバー。  
   * 受け取ったジョブをSQLiteに保存し、バックグラウンドのディスパッチャが**宛先ごとの設定値に基づいて実行可能か判断**してスレッドプールへ送ります。  
   * ジョブ完了時、メモリ上の threading.Event を発火させ、待機中の HTTP リクエストにレスポンスを返します。  
3. **Database (jobs.db)**  
   * SQLiteデータベース。  
4. **Config (queue\_config.json)**  
   * ポート番号やレートリミット設定（デフォルトおよび個別）を定義するファイル。

### **3.2. データフロー**

\[Client\]                                  \[Worker Daemon (Singleton)\]                 \[SQLite\]  
   │                                             │                                       │  
   ├── 1\. POST /api/jobs (接続失敗)              │                                       │  
   ├── 2\. 起動(subprocess.Popen) ───────────────\>│ (ポートバインド＆初期化)              │  
   │                                             │                                       │  
   ├── 3\. POST /api/jobs (成功) ────────────────\>│──(INSERT)────────────────────────────\>│  
   │                                             │                                       │  
   ├── 4\. GET /api/jobs/{id}/wait ──────────────\>│ (Event.wait() でスレッド待機)         │  
   │      (ロングポーリング開始)                 │                                       │  
   │                                             │──(Dispatcher Loop)                    │  
   │                                             │   ├── Endpoint Aの処理可否チェック    │  
   │                                             │   ├── Endpoint Bの処理可否チェック    │  
   │                                             │   └── 外部MCPサーバーへSubmit         │  
   │                                             │                                       │  
   │                                             │──(UPDATE: completed & result)────────\>│  
   │                                             │ (Event.set() で待機スレッド起床)      │  
   │\<─ 5\. 200 OK (結果返却) ─────────────────────┤                                       │

## **4\. 詳細設計**

### **4.1. 設定ファイル (queue\_config.json)**

プロジェクトルートまたはスキルディレクトリに配置します。エンドポイントごとに異なる制限値を定義できるよう、default\_rate\_limit と endpoint\_rate\_limits を分離します。

{  
  "host": "127.0.0.1",  
  "port": 54321,  
  "idle\_timeout\_seconds": 60,  
  "default\_rate\_limit": {  
    "max\_concurrent\_jobs": 2,  
    "min\_interval\_seconds": 2.0  
  },  
  "endpoint\_rate\_limits": {  
    "http://localhost:8000": {  
      "max\_concurrent\_jobs": 1,  
      "min\_interval\_seconds": 10.0  
    },  
    "\[http://192.168.1.50:8001\](http://192.168.1.50:8001)": {  
      "max\_concurrent\_jobs": 5,  
      "min\_interval\_seconds": 0.5  
    }  
  }  
}

### **4.2. SQLite スキーマ (jobs.db)**

スキーマレス（パススルー）なアプローチにより、ペイロード（args）をJSON文字列として格納し、外部MCPサーバーの任意の引数構造を吸収します。

CREATE TABLE IF NOT EXISTS jobs (  
    id TEXT PRIMARY KEY,  
    endpoint TEXT NOT NULL,  
    submit\_tool TEXT NOT NULL,  
    args TEXT NOT NULL,  
    status\_tool TEXT,  
    result\_tool TEXT,  
    headers TEXT,  
    status TEXT DEFAULT 'pending', \-- pending, running, completed, failed  
    result TEXT,  
    created\_at DATETIME DEFAULT CURRENT\_TIMESTAMP,  
    updated\_at DATETIME DEFAULT CURRENT\_TIMESTAMP  
);

### **4.3. Worker API (HTTP REST)**

* **GET /api/health**: 死活監視および起動完了確認。  
* **POST /api/jobs**: ジョブの登録。  
* **GET /api/jobs/{job\_id}/wait （最重要エンドポイント）**:  
  ジョブの完了をブロックして待つ（ロングポーリング）。  
  1. メモリ上のジョブ管理辞書から threading.Event を取得。  
  2. Event.wait(timeout=300) で待機（この間DBアクセスなし）。  
  3. Eventがセットされたら、DBから1度だけ最新の結果を読み取り 200 OK で返す。

### **4.4. 内部キューイングとレートリミット（エンドポイント別）**

Workerのバックグラウンドディスパッチャは以下のロジックで稼働し、**各MCPサーバーの固有の制限値に従って**独立して制御します。

1. ディスパッチャはメモリ上に「エンドポイントごとの最終実行時刻（last\_run\_time\[endpoint\]）」を記録する辞書を持つ。  
2. ループ内で、現在 pending または running のジョブが存在する**ユニークなエンドポイントの一覧**を取得する。  
3. **各エンドポイントに対して以下を評価する:**  
   * 設定ファイルから、そのエンドポイントの max\_concurrent\_jobs と min\_interval\_seconds を決定する。（endpoint\_rate\_limits に指定がなければ default\_rate\_limit を使用）  
   * 現在そのエンドポイント向けに実行中（running）のジョブ数をカウントする。  
   * running の数が決定した max\_concurrent\_jobs 以上なら、空きが出るまでこのエンドポイントの pending ジョブはスキップする。  
   * 現在時刻と last\_run\_time\[endpoint\] の差分が決定した min\_interval\_seconds 未満なら、インターバルを満たすまでこのエンドポイントのジョブはスキップする。  
4. 条件をクリアしたエンドポイントについて、最も古い pending ジョブを1つ取得して running に更新し、ThreadPoolExecutor に submit する。同時に last\_run\_time\[endpoint\] を現在時刻に更新する。  
5. 短いスリープ（例: time.sleep(0.1)）を挟んでループを繰り返す。

### **4.5. シングルトンと自動起動・終了**

* **シングルトン**: http.server 起動時の socket.bind() でポートが使用中の場合 OSError となるため、別のワーカーが生きていると判断して安全に終了します。  
* **自動起動 (Client側)**:  
  OS非依存となるよう、実行環境に合わせてプロセスの切り離し方（フラグ）を分岐させます。  
  import subprocess  
  import sys

  kwargs \= {}  
  if sys.platform \== "win32":  
      \# Windowsの場合：コンソールを出さず、プロセスを完全に切り離す  
      kwargs\["creationflags"\] \= subprocess.CREATE\_NO\_WINDOW | subprocess.DETACHED\_PROCESS  
  else:  
      \# macOS / Linuxの場合：新しいセッションを作成し、親プロセスから切り離す  
      kwargs\["start\_new\_session"\] \= True

  subprocess.Popen(\[sys.executable, "mcp\_worker\_daemon.py"\], \*\*kwargs)

* **自動終了 (Worker側)**:  
  ディスパッチャループ内で「稼働中の全ジョブ数」と「最終APIアクセス時刻」を監視し、ジョブが0でかつ設定されたアイドル時間を超過した場合、サーバーをシャットダウンします。

## **5\. 実装計画（テスト駆動開発: TDD）**

本プロジェクトはコンポーネントが多いため、依存関係の少ない内側からTDDで実装を進めます。

* **Step 1: データベース・モデル層 (db.py)**  
  * インメモリDB (:memory:) を使用し、ジョブのCRUD操作とステータス更新をテスト。  
* **Step 2: キューディスパッチャとレートリミット (dispatcher.py)**  
  * ダミーのHTTPリクエスト関数（モック）を注入。  
  * **(重要)** 異なる2つのエンドポイント（AとB）宛のジョブを投入し、Aには並行数1/間隔長め、Bには並行数5/間隔短めの設定を与え、それぞれの制限に沿って独立して処理されるかをテスト。  
* **Step 3: Worker HTTP層 (mcp\_worker\_daemon.py)**  
  * テスト用ポートでサーバーを起動し、API経由でのジョブ登録と /wait によるロングポーリングの正常性をテスト。  
  * アイドルタイムアウトで自動終了するかテスト。  
* **Step 4: フロントクライアント (mcp\_async\_call.py 改修)**  
  * Workerが落ちている状態でリクエストを開始し、自動起動ロジックが各OSで正常に走るかテスト。