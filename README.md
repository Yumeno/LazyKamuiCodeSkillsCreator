# KamuiCodeSkillsCreator

> **Fork元**: このリポジトリは [えるさん (@el_el_san)](https://x.com/el_el_san) 開発の [KamuiCodeSkillsCreator](https://github.com/el-el-san/KamuiCodeSkillsCreator) のフォークです。
> 開発記事: [note.com](https://note.com/el_el_san/n/n6d160cbe88ad?sub_rt=share_pb)

Claude Code用のMCPスキルジェネレーター。非同期ジョブパターン（submit/status/result）を使用するHTTP MCPサーバーからスキルを生成します。

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
# カタログから自動取得（推奨）
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  -m /path/to/your/.mcp.json

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
├── scripts/
│   ├── mcp_async_call.py # コア非同期コーラー
│   └── <skill_name>.py   # 便利ラッパー
└── references/
    ├── mcp.json          # 元のMCPコンフィグ
    └── tools.json        # 元のツール仕様
```

**Lazyモード (`--lazy`):**
```
.claude/skills/<skill-name>/
├── SKILL.md              # 使用方法ドキュメント（軽量版）
├── scripts/
│   ├── mcp_async_call.py # コア非同期コーラー
│   └── <skill_name>.py   # 便利ラッパー
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
- AIは実行前に `references/tools.json` を読み込んで詳細を確認
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

```json
{
  "name": "my-mcp-server",
  "url": "https://mcp.example.com/sse",
  "type": "url"
}
```

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
| `--poll-interval` | ポーリング間隔秒数（デフォルト: 2.0） |
| `--max-polls` | 最大ポーリング回数（デフォルト: 300） |
| `--header` | カスタムヘッダー追加（形式: `Key:Value`） |
| `--config, -c` | .mcp.jsonからエンドポイントを読み込み |

### `scripts/generate_skill.py`

MCP仕様から完全なスキルを生成。

**オプション:**
| オプション | 説明 |
|-----------|------|
| `--mcp-config, -m` | .mcp.jsonへのパス（必須） |
| `--tools-info, -t` | tools.infoへのパス（レガシーモード、省略時はカタログから取得） |
| `--output, -o` | 出力ディレクトリ（デフォルト: .claude/skills） |
| `--name, -n` | スキル名（省略時は自動検出） |
| `--catalog-url` | カタログYAMLのURL（デフォルト: GitHub） |
| `--lazy, -l` | 最小限のSKILL.mdを生成（ツール定義は references/tools.json に委譲） |

## 生成されるスキル構造

```
skill-name/
├── SKILL.md              # 使用方法ドキュメント
├── scripts/
│   ├── mcp_async_call.py # コア非同期コーラー
│   └── skill_name.py     # 便利ラッパー
└── references/
    ├── mcp.json          # 元のMCPコンフィグ
    └── tools.json        # 元のツール仕様
```

## ステータス値一覧

| ステータス | 意味 |
|-----------|------|
| `pending`, `queued` | ジョブ待機中 |
| `processing`, `running` | 処理中 |
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
    output_path="./output",
    poll_interval=2.0,
    max_polls=300,
)

print(result["saved_path"])  # ダウンロードしたファイルへのパス
```

## エラーハンドリング

スクリプトは以下を処理します：
- レスポンス内のJSON-RPCエラー
- ジョブ失敗（status: failed/error）
- 最大ポーリング後のタイムアウト
- ダウンロード失敗

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
