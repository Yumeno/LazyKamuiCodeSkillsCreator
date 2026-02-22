# LazyKamuiCodeSkillsCreator

> **Fork元**: このリポジトリは [えるさん (@el_el_san)](https://x.com/el_el_san) 開発の [KamuiCodeSkillsCreator](https://github.com/el-el-san/KamuiCodeSkillsCreator) のフォークです。
> 開発記事: [note.com](https://note.com/el_el_san/n/n6d160cbe88ad?sub_rt=share_pb)

Claude Code用のMCPスキルジェネレーター。非同期ジョブパターン（submit/status/result）を使用するHTTP MCPサーバーからスキルを生成します。

## 🆕 このフォークの追加機能

オリジナル版からの主な機能追加：

| 機能 | 説明 | オプション | 詳細 |
|-----|------|----------|------|
| **Lazyモード** | SKILL.mdを軽量化し、ツール定義を外部YAMLファイルに分離。初期コンテキスト消費を大幅削減 | `--lazy` | [📖](docs/lazy-mode.md) |
| **複数サーバー対応** | 1つのmcp.jsonから複数サーバーのスキルを一括生成。個別指定も可能 | `--servers` | [📖](docs/lazy-mode.md) |
| **YAML形式出力** | ツール定義をLLMフレンドリーなYAML形式で出力。`_usage`セクションに実行例を含む | Lazyモード時自動 | [📖](docs/lazy-mode.md) |
| **スキーマ詳細保持** | enum/default/min/max等のJSON Schema情報を完全保持。LLMがパラメータ制約を理解可能に | 自動 | [📖](docs/schema-passthrough.md) |
| **出力ファイル指定** | ディレクトリとファイルパスを別々に指定可能。ファイル名のみ指定で組み合わせ | `--output-file` | [📖](docs/output-path-strategy.md) |
| **自動ファイル命名** | `{request_id}_{timestamp}.{ext}` 形式でユニークなファイル名を自動生成 | `--auto-filename` | [📖](docs/output-path-strategy.md) |
| **拡張子自動検出** | Content-Type、URL、ユーザー指定の優先順位で拡張子を決定 | 自動 | [📖](docs/output-path-strategy.md) |
| **重複ファイル回避** | 同名ファイル存在時にサフィックス自動付与（`_1`, `_2`...） | 自動 | [📖](docs/output-path-strategy.md) |
| **ログ保存** | リクエスト/レスポンスJSONを保存（logsフォルダまたはインライン） | `--save-logs`, `--save-logs-inline` | [📖](docs/output-path-strategy.md) |
| **複数ファイル対応** | レスポンス内の全URLを再帰探索し一括ダウンロード。連番サフィックス自動付与 | 自動 | [📖](docs/output-path-strategy.md) |
| **キューシステム** | ローカルワーカーによるエンドポイント別レートリミットと並行数制御。自動起動・自動終了 | `--queue-config` | [📖](docs/queue_system_design.md) |
| **キュー堅牢化** | ゾンビジョブ回復・指数バックオフリトライ・429 Retry-After対応・ワーカー側ダウンロード | 自動 | [📖](docs/queue_system_hardening.md) |

### ⚠️ 実行ディレクトリについて

生成されたスキルは**プロジェクトルートから**実行してください：

```bash
# ✓ 正しい（プロジェクトルートから）
python .claude/skills/{skill-name}/scripts/mcp_async_call.py \
  --output ./save_dir  # → /project/save_dir/ に保存

# ✗ 避ける（スキルディレクトリから）
cd .claude/skills/{skill-name}
python scripts/mcp_async_call.py \
  --output ./save_dir  # → /project/.claude/skills/{skill-name}/save_dir/ に保存
```

> 📖 詳細: [出力パス戦略](docs/output-path-strategy.md)

### 機能比較

```
オリジナル版:
  mcp.json → [1サーバー] → SKILL.md（全ツール詳細埋め込み）+ tools.json

このフォーク版:
  mcp.json → [複数サーバー対応] → 各サーバーごとにスキル生成
                ↓
           通常モード: SKILL.md（全詳細）+ tools.json
           Lazyモード: SKILL.md（軽量）+ tools/{skill}.yaml（実行例付き）
```

### Lazyモードのメリット

- **トークン節約**: SKILL.mdに全パラメータを埋め込まないため、初期読み込み時のトークン消費を削減
- **実行時に必要な情報のみ取得**: AIが実行前にYAMLを読むことで、必要なツールの情報だけを取得
- **自己完結型YAML**: `_usage`セクションに実行コマンド例が含まれるため、YAMLファイル1つで実行可能

## 概要

このツールは以下の用途に使用できます：

- `.mcp.json` からパッケージ化されたスキルを生成（ツール情報はカタログから自動取得）
- 非同期MCPツールの呼び出し：submit → ステータスポーリング → 結果取得 → ダウンロード
- 画像/動画生成MCP（fal.ai、Replicateなど）の統合

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/Yumeno/LazyKamuiCodeSkillsCreator.git
cd LazyKamuiCodeSkillsCreator
```

### 2. 依存パッケージをインストール

```bash
pip install pyyaml requests
```

### 3. スキルを生成

```bash
# mcp.json内の全サーバーのスキルを生成
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  -m /path/to/your/.mcp.json

# 特定のサーバーのみ生成
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  -m /path/to/your/.mcp.json \
  -s fal-ai/flux-lora

# 複数サーバーを指定
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  -m /path/to/your/.mcp.json \
  -s fal-ai/flux-lora -s fal-ai/video-enhance

# Lazyモードで生成（コンテキスト節約）
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  -m /path/to/your/.mcp.json \
  --lazy
```

### 4. 生成されたスキルの場所

**通常モード:**
```
.claude/skills/<skill-name>/
├── SKILL.md              # 使用方法ドキュメント（全ツール詳細含む）
├── queue_config.json     # キュー設定（レートリミット等）
├── scripts/
│   ├── mcp_async_call.py # コア非同期コーラー
│   ├── mcp_worker_daemon.py # キューワーカーデーモン
│   ├── <skill_name>.py   # 便利ラッパー
│   └── job_queue/        # キューシステムパッケージ
│       ├── db.py         # SQLiteジョブストア
│       ├── dispatcher.py # エンドポイント別レートリミット
│       ├── worker.py     # HTTP REST APIサーバー
│       └── client.py     # キュークライアント
└── references/
    ├── mcp.json          # 元のMCPコンフィグ
    └── tools.json        # 元のツール仕様
```

**Lazyモード (`--lazy`):**
```
.claude/skills/<skill-name>/
├── SKILL.md              # 使用方法ドキュメント（軽量版）
├── queue_config.json     # キュー設定（レートリミット等）
├── scripts/
│   ├── mcp_async_call.py # コア非同期コーラー
│   ├── mcp_worker_daemon.py # キューワーカーデーモン
│   ├── <skill_name>.py   # 便利ラッパー
│   └── job_queue/        # キューシステムパッケージ
└── references/
    ├── mcp.json          # 元のMCPコンフィグ
    └── tools/
        └── <skill-name>.yaml  # ツール定義+使用例（YAML形式）
```

## クイックスタート

### MCPコンフィグからスキルを生成（推奨）

ツール情報は `mcp_tool_catalog.yaml` から自動取得されます：

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json
```

### Lazyモード（コンテキスト節約）

ツール数が多いMCPサーバーでは、`--lazy` オプションで初期コンテキスト消費を削減できます：

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --lazy
```

**Lazyモードの動作:**
- SKILL.md にはツール名と説明のみを記載（パラメータ詳細は省略）
- AIは実行前に `references/tools/{skill}.yaml` を読み込んで詳細を確認
- 初期ロード時のトークン消費を大幅に削減

### レガシーモード（tools.info使用）

ローカルの `tools.info` ファイルを使用する場合：

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --tools-info /path/to/tools.info \
  --name my-mcp-skill
```

### 非同期ツールの直接呼び出し

```bash
python scripts/mcp_async_call.py \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "generate_image" \
  --status-tool "check_status" \
  --result-tool "get_result" \
  --args '{"prompt": "かわいい猫"}' \
  --output ./output
```

## 非同期パターンのフロー

```
1. SUBMIT    → JSON-RPC POST → session_id取得
2. STATUS    → session_idでポーリング → "completed"まで待機
3. RESULT    → ダウンロードURL取得
4. DOWNLOAD  → ローカルにファイル保存
```

## JSON-RPC 2.0 フォーマット

すべてのMCP呼び出しは以下の構造を使用します：

```json
{
  "jsonrpc": "2.0",
  "id": "unique-id",
  "method": "tools/call",
  "params": {
    "name": "tool_name",
    "arguments": { "key": "value" }
  }
}
```

## 入力ファイル形式

### .mcp.json

**単一サーバー形式:**
```json
{
  "name": "my-mcp-server",
  "url": "https://mcp.example.com/sse",
  "type": "url"
}
```

**複数サーバー形式（推奨）:**
```json
{
  "mcpServers": {
    "fal-ai/flux-lora": {
      "url": "https://mcp.example.com/flux-lora/sse",
      "headers": {
        "Authorization": "Bearer xxx"
      }
    },
    "fal-ai/video-enhance": {
      "url": "https://mcp.example.com/video-enhance/sse",
      "headers": {
        "Authorization": "Bearer xxx"
      }
    }
  }
}
```

複数サーバー形式の場合：
- `python generate_skill.py -m mcp.json` → 全サーバーのスキルを生成
- `python generate_skill.py -m mcp.json -s fal-ai/flux-lora` → 指定サーバーのみ生成
- `python generate_skill.py -m mcp.json -s server1 -s server2` → 複数指定可能

### tools.info

```json
[
  {
    "name": "generate",
    "description": "コンテンツを生成",
    "inputSchema": {
      "type": "object",
      "properties": {
        "prompt": { "type": "string", "description": "入力プロンプト" }
      },
      "required": ["prompt"]
    }
  }
]
```

## スクリプトリファレンス

### `scripts/mcp_async_call.py`

フルフロー自動化を備えたメインの非同期MCPコーラー。

**オプション:**
| オプション | 説明 |
|-----------|------|
| `--endpoint, -e` | MCPサーバーURL |
| `--submit-tool` | ジョブ送信用ツール名 |
| `--status-tool` | ステータス確認用ツール名 |
| `--result-tool` | 結果取得用ツール名 |
| `--args, -a` | JSON文字列として送信引数 |
| `--args-file` | JSONファイルから引数を読み込み |
| `--output, -o` | 出力ディレクトリ（デフォルト: ./output） |
| `--output-file, -O` | 出力ファイルパス（上書き許可、ファイル名のみなら--outputと組み合わせ） |
| `--auto-filename` | `{request_id}_{timestamp}.{ext}` 形式で自動命名 |
| `--poll-interval` | ポーリング間隔秒数（デフォルト: 2.0） |
| `--max-polls` | 最大ポーリング回数（デフォルト: 300） |
| `--header` | カスタムヘッダー追加（形式: `Key:Value`） |
| `--config, -c` | .mcp.jsonからエンドポイントを読み込み |
| `--save-logs` | `{output}/logs/` にリクエスト/レスポンスログを保存 |
| `--save-logs-inline` | 出力ファイルと同じ場所に `{filename}_*.json` 形式でログ保存 |
| `--queue-config` | queue_config.jsonへのパス（キューモード有効化） |
| `--worker-url` | ワーカーURL（デフォルト: queue_config.jsonから取得） |
| `--submit-only` | ジョブを投入してjob_idを即座に返却 |
| `--wait JOB_ID` | 指定ジョブIDの状態を確認 |
| `--list` | キュー内の全ジョブを一覧（JSON出力） |
| `--stats` | エンドポイント別統計情報を表示 |
| `--filter-status` | `--list`使用時にステータスでフィルタ |

**拡張子の決定順序:**
1. `--output-file` で指定されている場合はその拡張子
2. ダウンロード時の `Content-Type` ヘッダーから推測
3. URLのパスから抽出
4. 検出できない場合は警告を表示

**重複ファイル回避:**
`--output-file` 未指定の場合、同名ファイルが存在するとサフィックスを付与:
- `output.png` → `output_1.png` → `output_2.png`

### `scripts/generate_skill.py`

MCP仕様から完全なスキルを生成。

**オプション:**
| オプション | 説明 |
|-----------|------|
| `--mcp-config, -m` | .mcp.jsonへのパス（必須） |
| `--servers, -s` | 生成するサーバー名（複数指定可、省略時は全サーバー） |
| `--tools-info, -t` | tools.infoへのパス（レガシーモード、単一サーバーのみ） |
| `--output, -o` | 出力ディレクトリ（デフォルト: .claude/skills） |
| `--name, -n` | スキル名（省略時は自動検出、単一サーバーのみ） |
| `--catalog-url` | カタログYAMLのURL（デフォルト: GitHub） |
| `--lazy, -l` | 最小限のSKILL.mdを生成（ツール定義は references/tools/*.yaml に委譲） |

## 生成されるスキル構造

```
skill-name/
├── SKILL.md              # 使用方法ドキュメント
├── queue_config.json     # キュー設定（レートリミット等）
├── scripts/
│   ├── mcp_async_call.py # コア非同期コーラー
│   ├── mcp_worker_daemon.py # キューワーカーデーモン
│   ├── skill_name.py     # 便利ラッパー
│   └── job_queue/        # キューシステムパッケージ
└── references/
    ├── mcp.json          # 元のMCPコンフィグ
    └── tools.json        # 元のツール仕様
```

## ステータス値一覧

| ステータス | 意味 |
|-----------|------|
| `pending`, `queued` | ジョブ待機中 |
| `processing`, `running` | 処理中 |
| `polling` | リモートMCPに送信済み、ポーリング中 |
| `recovering` | ワーカー再起動後の回復待ち |
| `completed`, `done`, `success` | 完了 |
| `failed`, `error` | 失敗 |

## プログラムからの使用

```python
from scripts.mcp_async_call import run_async_mcp_job

result = run_async_mcp_job(
    endpoint="https://mcp.example.com/sse",
    submit_tool="generate",
    submit_args={"prompt": "山に沈む夕日"},
    status_tool="status",
    result_tool="result",
    output_dir="./output",
    poll_interval=2.0,
    max_polls=300,
)

print(result["saved_path"])  # ダウンロードしたファイルへのパス
```

## キューシステム

生成されたスキルには、AIエージェントが並列にツールを呼び出した際のMCPサーバー過負荷を防ぐローカルキューシステムが含まれます。

### 仕組み

- ローカルワーカーデーモン（port 54321）がHTTP APIでジョブを受け付け、エンドポイント別にレートリミットを適用
- 初回使用時に自動起動、アイドルタイムアウト（デフォルト: 60秒）で自動終了
- SQLiteでジョブ状態を永続化。複数スキルで共有キューディレクトリ（`.claude/queue/`）を使用

### キューモード

```bash
# 送信して結果を待つ（デフォルト、従来互換）
python skill_name.py --args '{"prompt": "..."}'

# 送信のみ - job_idを即座に返す
python skill_name.py --submit-only --args '{"prompt": "..."}'

# ジョブ状態を確認
python mcp_async_call.py --queue-config ../queue_config.json --wait JOB_ID

# キュー内ジョブ一覧
python mcp_async_call.py --queue-config ../queue_config.json --list

# エンドポイント別統計
python mcp_async_call.py --queue-config ../queue_config.json --stats
```

### 堅牢化機能

| 機能 | 説明 |
|-----|------|
| **ゾンビジョブ回復** | ワーカー再起動時、処理中だったジョブを自動的に回復（polling+remote_job_id有り→recovering）または失敗マーク |
| **指数バックオフリトライ** | 接続エラー・503/504に対し2s→4s→8sのバックオフで自動リトライ（最大3回） |
| **429 Retry-After対応** | 429レスポンスのRetry-Afterヘッダーを尊重し、エンドポイント単位で一時停止 |
| **ワーカー側ダウンロード** | 結果ファイルをワーカーがダウンロードし `results/{job_id}/` に保存 |
| **古いジョブ自動削除** | 起動時に指定期間超過のジョブをDB・結果ファイルごと自動削除（デフォルト: 24時間） |
| **設定マージ保護** | スキル再生成時にqueue_config.jsonのユーザーカスタマイズを保持 |

### 設定例 (queue_config.json)

```json
{
  "port": 54321,
  "idle_timeout_seconds": 60,
  "default_rate_limit": {
    "max_concurrent_jobs": 2,
    "min_interval_seconds": 2.0
  },
  "endpoint_rate_limits": {
    "http://slow-server:8000": {
      "max_concurrent_jobs": 1,
      "min_interval_seconds": 10.0
    }
  },
  "job_retention_seconds": 86400,
  "results_dir": ".claude/queue/results"
}
```

> 📖 詳細: [キューシステム設計](docs/queue_system_design.md) / [堅牢化設計](docs/queue_system_hardening.md)

## エラーハンドリング

スクリプトは以下を処理します：
- レスポンス内のJSON-RPCエラー
- ジョブ失敗（status: failed/error）
- 最大ポーリング後のタイムアウト
- ダウンロード失敗
- 接続エラー時の自動リトライ（指数バックオフ）
- 429 Too Many Requests（Retry-After尊重）
- ゾンビジョブの自動回復・失敗マーク

すべてのエラーは説明的なメッセージを含む例外を発生させます。

## Lazyモード詳細

### 通常モード vs Lazyモード

| 項目 | 通常モード | Lazyモード |
|-----|-----------|-----------|
| SKILL.mdのサイズ | 大（パラメータ詳細含む） | 小（名前+説明のみ） |
| ツール定義の形式 | JSON（tools.json） | YAML（tools/{skill}.yaml） |
| 初期トークン消費 | 高 | 極小 |
| ツール実行までのステップ | 即実行可能 | +1ターン（YAML読み込み） |
| 推奨用途 | ツール数が少ない場合 | ツール数が多い場合 |

### Lazyモードの使用フロー

1. ユーザーがAIに指示（例：「画像を生成して」）
2. AIがSKILL.mdを確認し、該当ツールを特定
3. AIが `references/tools/{skill}.yaml` を読み込んでパラメータと実行方法を確認
4. AIがツールを実行

### 生成されるYAMLの例（Lazyモード）

```yaml
# references/tools/t2i-kamui-fal-flux-lora.yaml
_usage:
  description: How to execute this MCP server's tools
  bash: |
    python scripts/mcp_async_call.py \
      --endpoint "https://kamui-code.ai/t2i/fal/flux-lora" \
      --submit-tool "flux_lora_submit" \
      --status-tool "flux_lora_status" \
      --result-tool "flux_lora_result" \
      --args '{"prompt": "your input here"}' \
      --header "KAMUI-CODE-PASS:your-pass" \
      --output ./output
  wrapper: python scripts/t2i_kamui_fal_flux_lora.py --args '{"prompt": "..."}'

flux_lora_submit:
  description: Submit Flux LoRA image generation request
  required:
    - prompt
  parameters:
    prompt:
      type: string
      description: Image prompt
    lora_path:
      type: string
      description: LoRA model path

flux_lora_status:
  description: Check job status
  required:
    - request_id
  parameters:
    request_id:
      type: string
      description: Request ID from submit

flux_lora_result:
  description: Get generation result
  required:
    - request_id
  parameters:
    request_id:
      type: string
      description: Request ID
```

AIはこのYAMLファイル1つを読むだけで、実行に必要な情報をすべて取得できます。

## ライセンス

MIT License
