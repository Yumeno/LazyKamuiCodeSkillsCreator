# 出力パス戦略

## 目的

ダウンロードファイルの保存先パス・ファイル名・拡張子を柔軟かつ直感的に制御するための仕組みを提供します。

### 背景・課題

オリジナル実装では `--output` オプションが「ディレクトリ」と「ファイルパス」の両方に使われており、以下の問題がありました：

1. **曖昧な動作**: `--output ./result` がディレクトリなのかファイル名なのか不明確
2. **拡張子欠落**: ファイルが拡張子なしで保存されることがある
3. **上書き問題**: 同名ファイルが意図せず上書きされる
4. **デバッグ困難**: リクエスト/レスポンスのログが残らない

### 解決アプローチ

Linux CLIツール（curl, wget等）の慣例に従い、役割を明確に分離：

- `--output` (`-o`): 出力**ディレクトリ**指定
- `--output-file` (`-O`): 出力**ファイルパス**指定
- Content-Typeベースの拡張子自動検出
- 重複ファイル回避の自動サフィックス付与

## 最終要件

### 出力パス決定

1. `--output-file` 指定時はそのパスを使用（上書き許可）
2. 未指定時は `--output` ディレクトリ + 自動生成ファイル名
3. ファイル名のみの `--output-file` は `--output` と組み合わせ

### 拡張子決定

優先順位：
1. `--output-file` で明示的に指定された拡張子
2. ダウンロード時の `Content-Type` ヘッダーから推測
3. URLのパスから抽出
4. 検出できない場合は警告を表示（拡張子なし）

### ファイル名決定

優先順位：
1. `--output-file` で明示的に指定
2. `--auto-filename` 有効時: `{request_id}_{timestamp}.{ext}`
3. レスポンスの `Content-Disposition` ヘッダー
4. URLのパスから抽出
5. `request_id` があれば使用
6. フォールバック: `output`

### 重複回避

`--output-file` **未指定**の場合のみ、同名ファイル存在時にサフィックス付与：
- `output.png` → `output_1.png` → `output_2.png` → ...

## 機能設計

### CLIオプション

| オプション | 説明 | デフォルト |
|-----------|------|-----------|
| `--output, -o` | 出力ディレクトリ | `./output` |
| `--output-file, -O` | 出力ファイルパス（上書き許可） | なし |
| `--auto-filename` | `{request_id}_{timestamp}.{ext}` 形式 | 無効 |
| `--save-logs` | `{output}/logs/` にログ保存 | 無効 |
| `--save-logs-inline` | ファイル横にログ保存 | 無効 |

### パス解決ロジック

```
--output-file 指定あり？
  ├─ Yes: パス含む？
  │    ├─ Yes (絶対/相対パス): そのまま使用
  │    └─ No (ファイル名のみ): --output + ファイル名
  └─ No: --output + 自動生成ファイル名
              └─ 同名存在時: サフィックス付与
```

### Content-Type マッピング

```python
CONTENT_TYPE_MAP = {
    # Images
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",

    # Videos
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",

    # Audio
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "audio/webm": ".weba",

    # 3D Models
    "model/gltf-binary": ".glb",
    "model/gltf+json": ".gltf",
    "application/octet-stream": "",  # 拡張子推測不可

    # Documents
    "application/pdf": ".pdf",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",

    # Archives
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-tar": ".tar",
}
```

## 実装仕様詳細

### ヘルパー関数

#### `get_extension_from_content_type(content_type)`

```python
def get_extension_from_content_type(content_type: str) -> str:
    """Content-Typeヘッダーから拡張子を取得"""
    if not content_type:
        return ""
    # "image/png; charset=utf-8" → "image/png"
    mime_type = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_MAP.get(mime_type, "")
```

#### `get_extension_from_url(url)`

```python
def get_extension_from_url(url: str) -> str:
    """URLパスから拡張子を取得"""
    parsed = urlparse(url)
    path = parsed.path
    # クエリパラメータを除去
    if "?" in path:
        path = path.split("?")[0]
    _, ext = os.path.splitext(path)
    return ext.lower() if ext else ""
```

#### `get_unique_filepath(filepath)`

```python
def get_unique_filepath(filepath: str) -> str:
    """重複しないファイルパスを生成"""
    if not os.path.exists(filepath):
        return filepath

    base, ext = os.path.splitext(filepath)
    counter = 1
    while True:
        new_path = f"{base}_{counter}{ext}"
        if not os.path.exists(new_path):
            return new_path
        counter += 1
```

#### `resolve_output_path(output_dir, output_file, auto_filename, avoid_overwrite)`

```python
def resolve_output_path(
    output_dir: str | None,
    output_file: str | None,
    auto_filename: str,
    avoid_overwrite: bool = True
) -> str:
    """最終的な出力パスを決定"""
    if output_file:
        # output_file指定あり
        if os.path.isabs(output_file) or os.path.dirname(output_file):
            # フルパスまたは相対パス
            filepath = output_file
        else:
            # ファイル名のみ → output_dirと結合
            filepath = os.path.join(output_dir or ".", output_file)
        # output_file指定時は上書き許可（avoid_overwrite無視）
    else:
        # 自動生成ファイル名使用
        filepath = os.path.join(output_dir or "./output", auto_filename)
        if avoid_overwrite:
            filepath = get_unique_filepath(filepath)

    # ディレクトリ作成
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    return filepath
```

#### `generate_auto_filename(request_id, extension)`

```python
def generate_auto_filename(request_id: str | None, extension: str) -> str:
    """自動ファイル名を生成: {request_id}_{timestamp}.{ext}"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if request_id:
        # request_idが長い場合は短縮
        short_id = request_id[:8] if len(request_id) > 8 else request_id
        base = f"{short_id}_{timestamp}"
    else:
        base = f"output_{timestamp}"

    return f"{base}{extension}" if extension else base
```

### ログ保存

#### `--save-logs`: ディレクトリ保存

```
./output/
├── result.png
└── logs/
    ├── abc12345_request.json   # 送信したリクエスト
    └── abc12345_response.json  # 受信したレスポンス
```

#### `--save-logs-inline`: インライン保存

```
./output/
├── result.png
├── result_request.json
└── result_response.json
```

### ダウンロードフロー

```python
def download_file(url, output_dir, output_file, auto_filename_enabled, request_id, ...):
    # 1. ダウンロード実行
    response = requests.get(url, stream=True)

    # 2. 拡張子決定
    if output_file and os.path.splitext(output_file)[1]:
        # output_fileに拡張子あり
        extension = os.path.splitext(output_file)[1]
    else:
        # Content-Typeから取得
        content_type = response.headers.get("Content-Type", "")
        extension = get_extension_from_content_type(content_type)

        if not extension:
            # URLから取得
            extension = get_extension_from_url(url)

        if not extension:
            print("Warning: Could not detect file extension", file=sys.stderr)

    # 3. ファイル名決定
    if auto_filename_enabled:
        filename = generate_auto_filename(request_id, extension)
    elif output_file:
        filename = output_file
    else:
        # Content-Disposition or URL or request_id or "output"
        filename = get_filename_from_response(response, url, request_id) + extension

    # 4. パス解決
    filepath = resolve_output_path(output_dir, output_file, filename)

    # 5. 保存
    with open(filepath, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    return filepath
```

## 使用例

### 基本的な使用

```bash
# ディレクトリ指定（ファイル名は自動）
python mcp_async_call.py ... --output ./downloads

# 結果: ./downloads/output.png (または output_1.png, output_2.png...)
```

### ファイルパス指定

```bash
# フルパス指定（上書き許可）
python mcp_async_call.py ... --output-file ./results/my_image.png

# ファイル名のみ（--outputと組み合わせ）
python mcp_async_call.py ... --output ./downloads --output-file my_image.png

# 結果: ./downloads/my_image.png
```

### 自動ファイル命名

```bash
# request_idとタイムスタンプでユニーク名生成
python mcp_async_call.py ... --auto-filename

# 結果: ./output/abc12345_20250629_143052.png
```

### ログ保存

```bash
# logsフォルダに保存
python mcp_async_call.py ... --save-logs

# ファイル横に保存
python mcp_async_call.py ... --save-logs-inline
```

### 組み合わせ

```bash
# 全オプション組み合わせ
python mcp_async_call.py \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "generate" \
  --status-tool "status" \
  --result-tool "result" \
  --args '{"prompt": "a cat"}' \
  --output ./my_project/assets \
  --auto-filename \
  --save-logs-inline

# 結果:
# ./my_project/assets/abc12345_20250629_143052.png
# ./my_project/assets/abc12345_20250629_143052_request.json
# ./my_project/assets/abc12345_20250629_143052_response.json
```
