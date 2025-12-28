# KamuiCodeSkillsCreator

Claude Code用のMCPスキルジェネレーター。非同期ジョブパターン（submit/status/result）を使用するHTTP MCPサーバーからスキルを生成します。

## 概要

このツールは以下の用途に使用できます：

- `.mcp.json` + `tools.info` からパッケージ化されたスキルを生成
- 非同期MCPツールの呼び出し：submit → ステータスポーリング → 結果取得 → ダウンロード
- 画像/動画生成MCP（fal.ai、Replicateなど）の統合

## クイックスタート

### MCPコンフィグからスキルを生成

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --tools-info /path/to/tools.info \
  --output ./output \
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
| `--mcp-config, -m` | .mcp.jsonへのパス |
| `--tools-info, -t` | tools.infoへのパス |
| `--output, -o` | 出力ディレクトリ |
| `--name, -n` | スキル名（省略時は自動検出） |

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

## ライセンス

MIT License
