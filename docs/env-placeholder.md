# 環境変数プレースホルダー対応

## 1. 目的

mcp.json の認証ヘッダーに `${VAR_NAME}` 形式のプレースホルダーが記載されている場合に、生成時はそのまま保持し、実行時に環境変数または `.env` ファイルから値を解決する仕組みを提供します。

### 背景・課題

スキル開発の元となる mcp.json は、ユーザーまたは外部から提供されます。認証情報の記載方法として以下の2パターンが存在します:

**パターン1: 平文（従来通り）**
```json
{
  "headers": {
    "Authorization": "Bearer sk-xxxxx-actual-token"
  }
}
```

**パターン2: プレースホルダー（新規対応）**
```json
{
  "headers": {
    "PARAMETER_SAMPLE01": "${KEY_SAMPLE01_DUMMY}",
    "Authorization": "Bearer ${MCP_API_KEY}"
  }
}
```

パターン2のように、セキュリティ上の理由から提供元が認証情報の平文記載を避け、初めからプレースホルダー形式で記載している場合があります。

現在の実装では `load_mcp_config()` がヘッダー値をそのまま読み取り、生成されるファイル群（SKILL.md、ラッパースクリプト、YAML）にそのまま埋め込みます。平文であればこれで問題なく動作しますが、プレースホルダーの場合は `${VAR_NAME}` という文字列がそのまま認証ヘッダーとして送信されてしまい、認証に失敗します。

### 解決アプローチ

- 生成時（`generate_skill.py`）ではプレースホルダーを**一切解決せず**そのまま保持
- 実行時（`mcp_async_call.py`）で `--header` の値に含まれるプレースホルダーを検出し、環境変数 → `.env` ファイルの順で解決
- 平文のヘッダー値はこれまで通り無加工でパススルー

## 2. 要件定義

### 2.1. 機能要件

1. **プレースホルダー検出**: ヘッダー値内の `${VAR_NAME}` パターン（`VAR_NAME` は `[A-Za-z_][A-Za-z0-9_]*`）を検出できること
2. **実行時解決**: `mcp_async_call.py` の実行時に、ヘッダー値中のプレースホルダーを以下の優先順位で解決すること:
   1. `os.environ`（環境変数）
   2. `.env` ファイル（CWD → ホームディレクトリの順に探索）
3. **部分プレースホルダー対応**: `"Bearer ${MCP_API_KEY}"` のように値の一部がプレースホルダーの場合、プレースホルダー部分のみを解決し前後の文字列は保持すること
4. **複数プレースホルダー対応**: 1つのヘッダー値に複数のプレースホルダーが含まれる場合（例: `"${PREFIX}_${SUFFIX}"`）、全てを個別に解決すること
5. **生成時保持**: `generate_skill.py` が出力する全てのファイル（`references/mcp.json`、SKILL.md、YAML、ラッパースクリプト）でプレースホルダーをそのまま保持すること
6. **ドキュメントヒント**: 生成されたSKILL.mdやYAMLにプレースホルダーが含まれる場合、環境変数の設定方法に関するガイドを自動追記すること
7. **後方互換**: 平文ヘッダーの mcp.json はこれまで通り動作すること。プレースホルダーが1つも含まれない場合、追加処理は一切発生しないこと

### 2.2. 非機能要件

1. **標準ライブラリのみ**: プレースホルダー検出・解決・`.env` パースは Python 標準ライブラリ（`re`、`os`、`pathlib`）のみで実装すること。`python-dotenv` が利用可能な場合は優先的に使用するが、必須依存としないこと
2. **OS非依存**: Windows、macOS、Linux のいずれでも同一の挙動を保証すること
3. **対象範囲の限定**: プレースホルダー解決の対象は `headers` の**値部分のみ**とする。`url`、`name` 等の他フィールドは対象外とする

## 3. 機能設計

### 3.1. プレースホルダー書式

```
${VAR_NAME}
```

- 開始: `${`
- 変数名: `[A-Za-z_]` で始まり `[A-Za-z0-9_]*` が続く（標準的な環境変数命名規則）
- 終了: `}`

正規表現:
```python
_PLACEHOLDER_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
```

**対応する例:**
| 入力値 | 検出される変数名 |
|--------|---------------|
| `${MCP_API_KEY}` | `MCP_API_KEY` |
| `Bearer ${MCP_API_KEY}` | `MCP_API_KEY` |
| `${PREFIX}_${SUFFIX}` | `PREFIX`, `SUFFIX` |
| `sk-xxxxx` | （検出なし = 平文） |

**非対応:**
| 入力値 | 理由 |
|--------|------|
| `$VAR_NAME` | `${}` で囲まれていない |
| `${123VAR}` | 数字始まりの変数名 |
| `${VAR-NAME}` | ハイフンは変数名に使用不可 |

### 3.2. 解決優先順位

```
1. os.environ（環境変数）
    ↓ 見つからない場合
2. .env ファイル（CWD → ホームディレクトリの順に探索）
    ↓ 見つからない場合
3. エラー終了（sys.exit(1) + 不足変数名を表示）
```

### 3.3. .env ファイル仕様

サポートする書式:
```bash
# コメント行（無視される）
KEY=value
KEY="quoted value"
KEY='single quoted value'

# 値に = を含む場合
DATABASE_URL=postgres://user:pass@host/db

# 空行は無視される
```

- `python-dotenv` がインストール済みの場合はそちらを優先使用（マルチライン値、export接頭辞、変数展開などのリッチな機能が利用可能）
- 未インストールの場合はビルトインの簡易パーサーにフォールバック
- `.env` ファイルが存在しない場合はエラーにせず、`os.environ` のみで解決を試みる

### 3.4. データフロー

```
mcp.json（ユーザー提供、プレースホルダー入り）
    ↓ load_mcp_config()
all_headers = {"Authorization": "Bearer ${MCP_API_KEY}"}  ← プレースホルダーのまま
    ↓
[生成時] generate_skill.py
    ├── references/mcp.json    → プレースホルダーのまま保存
    ├── SKILL.md               → プレースホルダー表示 + 環境変数の説明追記
    ├── tools/*.yaml (_usage)  → --header "Authorization:Bearer ${MCP_API_KEY}" のまま
    └── wrapper.py DEFAULTS    → ("--header", "Authorization:Bearer ${MCP_API_KEY}")
    ↓
[実行時] wrapper.py → mcp_async_call.py --header "Authorization:Bearer ${MCP_API_KEY}"
    ↓ main() → header パース → resolve_header_placeholders()
    ↓ os.environ["MCP_API_KEY"] or .env の MCP_API_KEY を取得
headers = {"Authorization": "Bearer sk-actual-token-value"}  ← 解決済み
    ↓ run_async_mcp_job(headers=headers)
```

## 4. 実装仕様詳細

### 4.1. `mcp_async_call.py` の変更（ランタイム解決）

実行時にプレースホルダーを解決するロジックを追加します。生成された各スキルの `scripts/` ディレクトリにコピーされるため、自己完結型である必要があります。

#### 追加する正規表現（import 直後）

```python
import re

_PLACEHOLDER_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
```

#### `_parse_dotenv(path)` - ビルトイン .env パーサー

```python
def _parse_dotenv(path: Path) -> dict[str, str]:
    """標準ライブラリのみで .env ファイルをパースする。

    対応書式: KEY=VALUE, KEY="VALUE", KEY='VALUE', コメント行, 空行
    """
    result = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # 囲み引用符の除去
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if key:
                    result[key] = value
    except (OSError, UnicodeDecodeError):
        pass
    return result
```

#### `_load_dotenv_simple()` - .env ファイル探索・読み込み

```python
_dotenv_loaded = False
_dotenv_values = {}

def _load_dotenv_simple() -> dict[str, str]:
    """CWD → ホームディレクトリの順で .env ファイルを探索・読み込み。

    python-dotenv がインストール済みならそちらを優先使用する。
    結果はプロセス内でキャッシュされる。
    """
    global _dotenv_loaded, _dotenv_values
    if _dotenv_loaded:
        return _dotenv_values
    _dotenv_loaded = True

    search_dirs = [Path.cwd(), Path.home()]

    # python-dotenv が利用可能なら優先
    try:
        from dotenv import dotenv_values
        for search_dir in search_dirs:
            env_path = search_dir / ".env"
            if env_path.is_file():
                _dotenv_values = {
                    k: v for k, v in dotenv_values(env_path).items()
                    if v is not None
                }
                return _dotenv_values
    except ImportError:
        pass

    # フォールバック: ビルトインパーサー
    for search_dir in search_dirs:
        env_path = search_dir / ".env"
        if env_path.is_file():
            _dotenv_values = _parse_dotenv(env_path)
            return _dotenv_values

    return _dotenv_values
```

#### `resolve_header_placeholders(headers)` - メイン解決関数

```python
def resolve_header_placeholders(headers: dict[str, str]) -> dict[str, str]:
    """ヘッダー値中の ${VAR_NAME} プレースホルダーを解決する。

    解決順: os.environ → .env ファイル
    未解決の変数がある場合は sys.exit(1) でエラー終了。
    プレースホルダーを含まない値はそのままパススルー。
    """
    resolved = {}
    missing_vars = []

    for key, value in headers.items():
        if not _PLACEHOLDER_RE.search(value):
            resolved[key] = value
            continue

        dotenv = _load_dotenv_simple()

        def replacer(match):
            var_name = match.group(1)
            # 環境変数を優先
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            # .env ファイルにフォールバック
            dotenv_val = dotenv.get(var_name)
            if dotenv_val is not None:
                return dotenv_val
            # 未解決
            missing_vars.append(var_name)
            return match.group(0)

        resolved[key] = _PLACEHOLDER_RE.sub(replacer, value)

    if missing_vars:
        unique_missing = sorted(set(missing_vars))
        print(
            f"Error: Unresolved environment variable(s): {', '.join(unique_missing)}\n"
            f"Set them as environment variables or define in a .env file.",
            file=sys.stderr,
        )
        sys.exit(1)

    return resolved
```

#### `main()` への適用（1行追加）

既存の header パース処理の直後に1行追加するのみです:

```python
# Parse headers（既存コード）
headers = {"Content-Type": "application/json"}
if args.header:
    for h in args.header:
        key, val = h.split(":", 1)
        headers[key.strip()] = val.strip()

# ↓ この1行を追加
headers = resolve_header_placeholders(headers)
```

### 4.2. `generate_skill.py` の変更（ドキュメントヒント）

生成時にはプレースホルダーを解決せず、検出した場合にのみドキュメントへガイドを追記します。

#### プレースホルダー検出ヘルパー

`generate_skill.py` は既に `import re` を持つため、正規表現とヘルパー関数を追加します:

```python
_PLACEHOLDER_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')

def _has_placeholders(value: str) -> bool:
    """文字列に ${VAR_NAME} プレースホルダーが含まれるか判定する。"""
    return bool(_PLACEHOLDER_RE.search(value))

def _find_placeholders(value: str) -> list[str]:
    """文字列内の全プレースホルダー変数名を抽出する。"""
    return _PLACEHOLDER_RE.findall(value)
```

#### `generate_skill_md()` Authentication セクションの変更

プレースホルダーが検出された場合、環境変数の設定ガイドを追記します:

```python
# 既存: ヘッダー値の表示
headers_lines = "\n".join([f"{k}: {v}" for k, v in all_headers.items()])

# 追加: プレースホルダー検出時のガイドノート
env_note = ""
placeholder_vars = []
for v in all_headers.values():
    placeholder_vars.extend(_find_placeholders(v))
if placeholder_vars:
    unique_vars = sorted(set(placeholder_vars))
    vars_list = ', '.join(f'`{var}`' for var in unique_vars)
    env_note = f"""
> **Note:** Header values contain environment variable placeholders.
> Set these variables before execution or define them in a `.env` file:
> {vars_list}
"""
```

#### `convert_tools_to_yaml_dict()` の `_usage.notes` への追記

```python
# ヘッダーにプレースホルダーが含まれる場合、ノートを追加
if all_headers and any(_has_placeholders(v) for v in all_headers.values()):
    result["_usage"]["notes"]["env_vars"] = (
        "ヘッダーに環境変数プレースホルダー(${VAR})が含まれます。"
        "実行前に環境変数を設定するか、.envファイルに定義してください。"
    )
```

#### `generate_wrapper_script()` は変更なし

ラッパースクリプトの DEFAULTS にはプレースホルダーがそのまま埋め込まれます。解決は `mcp_async_call.py` 側で行われるため、ラッパーの変更は不要です:

```python
# ラッパーの DEFAULTS（生成される内容の例）
DEFAULTS = [
    ("--endpoint", "https://mcp.example.com/sse"),
    ("--submit-tool", "generate_image"),
    ("--header", "Authorization:Bearer ${MCP_API_KEY}"),  # ← そのまま保持
]

# main() で args に追加 → mcp_async_call.py に渡される
# → mcp_async_call.py の main() 内で resolve_header_placeholders() が解決
```

### 4.3. SKILL.md（ジェネレーター自身のドキュメント）

`.mcp.json` フォーマット説明セクションに、プレースホルダーの記法と `.env` ファイルの使い方を追記します。

追記内容:
- プレースホルダー付き mcp.json の記載例
- 解決優先順位の説明
- `.env` ファイルの書式例

## 5. エラーハンドリング

| シナリオ | 挙動 |
|---------|------|
| 環境変数にもなく .env にもない | `sys.exit(1)` + 不足変数名の一覧と設定方法を表示 |
| .env ファイルが存在しない | エラーにしない。`os.environ` のみで解決を試みる |
| .env ファイルのパースエラー | パース不能な行はスキップし、パース可能な行のみ使用 |
| .env ファイルの文字コードエラー | `UnicodeDecodeError` を捕捉し、`.env` を無視して続行 |
| ヘッダー値にプレースホルダーなし | 何もしない（追加処理ゼロ） |
| 部分プレースホルダー（`Bearer ${TOKEN}`） | `${TOKEN}` 部分のみ解決、`Bearer ` はそのまま保持 |
| `python-dotenv` 未インストール | ビルトインパーサーで `.env` を読み込み。機能的に問題なし |

**エラーメッセージの例:**

```
Error: Unresolved environment variable(s): MCP_API_KEY, CUSTOM_SECRET
Set them as environment variables or define in a .env file.
```

## 6. 変更ファイル一覧

| ファイル | 変更種別 | 変更内容 |
|---------|---------|---------|
| `scripts/mcp_async_call.py` | 修正 | 正規表現、.env パーサー、`resolve_header_placeholders()`、`main()` への1行追加（計 ~70行） |
| `scripts/generate_skill.py` | 修正 | `_has_placeholders()`、`_find_placeholders()`、Authentication セクションとYAML notes へのガイド追記（計 ~30行） |
| `SKILL.md` | 修正 | `.mcp.json` セクションにプレースホルダーの説明追加 |

**新規ファイルなし。** 解決ロジックは `mcp_async_call.py` に内蔵されるため、生成されたスキルの `scripts/` ディレクトリに自動的にコピーされます。

## 7. 実装計画

### Step 1: `mcp_async_call.py` にランタイム解決ロジック追加

1. `_PLACEHOLDER_RE` 正規表現を追加
2. `_parse_dotenv()` ビルトイン .env パーサーを追加
3. `_load_dotenv_simple()` .env 探索・キャッシュ関数を追加
4. `resolve_header_placeholders()` メイン解決関数を追加
5. `main()` の header パース直後に `headers = resolve_header_placeholders(headers)` を追加

### Step 2: `generate_skill.py` にドキュメントヒント追加

1. `_has_placeholders()` / `_find_placeholders()` ヘルパーを追加
2. `generate_skill_md()` の Authentication セクションにプレースホルダー検出時のガイドノートを追加
3. `convert_tools_to_yaml_dict()` の `_usage.notes` にプレースホルダー検出時の `env_vars` ノートを追加

### Step 3: SKILL.md にプレースホルダー使用方法の説明追加

1. `.mcp.json` フォーマット説明にプレースホルダー例を追加
2. 解決優先順位と `.env` ファイルの書式を記載

## 8. 検証方法

### 8.1. 生成時テスト（プレースホルダー保持の確認）

プレースホルダー付き mcp.json で `generate_skill.py` を実行し、出力ファイルを検査:

```json
{
  "mcpServers": {
    "test-server": {
      "url": "http://localhost:8000/sse",
      "headers": {
        "Authorization": "Bearer ${TEST_KEY}"
      }
    }
  }
}
```

確認項目:
- `references/mcp.json` に `${TEST_KEY}` が残ること
- SKILL.md の Authentication セクションに `${TEST_KEY}` が表示され、環境変数ガイドが追記されていること
- YAML の `_usage.bash` に `--header "Authorization:Bearer ${TEST_KEY}"` が残ること
- ラッパースクリプトの DEFAULTS に `("--header", "Authorization:Bearer ${TEST_KEY}")` が残ること

### 8.2. 実行時テスト（環境変数からの解決）

```bash
# 環境変数を設定
export TEST_KEY=my-secret-token

# 実行（エンドポイントは実際のサーバー不要、ヘッダー解決の確認のみ）
python mcp_async_call.py \
  --header "Authorization:Bearer ${TEST_KEY}" \
  --endpoint http://localhost:8000/sse \
  --submit-tool submit --status-tool status --result-tool result \
  --args '{}'
```

- 内部の headers dict が `{"Authorization": "Bearer my-secret-token"}` になることを確認

### 8.3. .env ファイルからの解決テスト

```bash
# .env ファイルを作成
echo 'TEST_KEY=from-dotenv-file' > .env

# 環境変数は未設定の状態で実行
unset TEST_KEY
python mcp_async_call.py --header "Authorization:Bearer ${TEST_KEY}" ...
```

- `.env` から `TEST_KEY=from-dotenv-file` が読み込まれ、解決されることを確認

### 8.4. 優先順位テスト

```bash
# 環境変数と .env の両方に同じ変数を設定
export TEST_KEY=from-env
echo 'TEST_KEY=from-dotenv' > .env

python mcp_async_call.py --header "Authorization:Bearer ${TEST_KEY}" ...
```

- 環境変数 (`from-env`) が優先されることを確認

### 8.5. 後方互換テスト

```json
{
  "headers": {
    "Authorization": "Bearer sk-actual-plaintext-token"
  }
}
```

- プレースホルダーなしの mcp.json で従来通り動作すること
- `.env` ファイルの有無に関わらず動作が変わらないこと

### 8.6. エラーケーステスト

```bash
# 環境変数未設定、.env なし
unset NONEXISTENT_VAR
python mcp_async_call.py --header "Authorization:Bearer ${NONEXISTENT_VAR}" ...
```

- `sys.exit(1)` でエラー終了し、不足変数名 `NONEXISTENT_VAR` が表示されること

## 9. 使用例

### 9.1. プレースホルダー付き mcp.json でのスキル生成

```bash
# ユーザーから提供された mcp.json（プレースホルダー入り）
cat my_server.mcp.json
# {
#   "mcpServers": {
#     "my-mcp-server": {
#       "url": "https://mcp.example.com/sse",
#       "headers": {
#         "Authorization": "Bearer ${MCP_API_KEY}"
#       }
#     }
#   }
# }

# スキル生成（プレースホルダーはそのまま保持される）
python scripts/generate_skill.py \
  --mcp-config my_server.mcp.json \
  --lazy
```

### 9.2. 環境変数を設定してスキル実行

```bash
# 環境変数を設定
export MCP_API_KEY=sk-xxxxx-your-actual-key

# ラッパースクリプト経由で実行
python .claude/skills/my-mcp-server/scripts/my_mcp_server.py \
  --args '{"prompt": "a landscape painting"}'
```

### 9.3. .env ファイルを使用

```bash
# プロジェクトルートに .env を作成
cat > .env << 'EOF'
MCP_API_KEY=sk-xxxxx-your-actual-key
CUSTOM_HEADER_VALUE=my-custom-value
EOF

# .env を .gitignore に追加（推奨）
echo '.env' >> .gitignore

# 環境変数の設定なしで実行（.env から自動的に読み込まれる）
python .claude/skills/my-mcp-server/scripts/my_mcp_server.py \
  --args '{"prompt": "a landscape painting"}'
```
