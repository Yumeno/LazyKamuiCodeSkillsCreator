# Worker / Client Version Handshake

`lazy-v2.11.0` 以降、worker daemon とクライアントスクリプトのバージョン不一致を **stderr に 1 回だけ** 警告する仕組みを追加しました。「上書きインストールしたが古い worker daemon が残っていて新機能が黙って無視される」事故を防ぐためのものです。

## なぜ必要か

このスキルは tar.gz を `.claude/skills/` 配下に展開するインストール形式です：

```bash
tar xzf mcp-async-skill.tar.gz -C .claude/skills/
```

よくある運用パスは「**古い worker daemon が port 54321 で動いたまま、新版を上書き展開する**」というもの。

このとき：

- 新しい `.py` ファイルは展開されているので、次の CLI 呼び出しから新クライアントが動く
- しかし **古い worker プロセスはまだ生きている**（idle timeout を待たないと自動終了しない）
- 新クライアントが新スキーマで PATCH を送る → 古い worker は知らないフィールドを **silent に drop**
- ユーザーは「設定したのに反映されない」と気付かないまま運用継続

PR2 (`#62`) の version handshake はこの skew を検知してユーザーに伝えます。

## 仕組み

### Worker 側

すべての JSON レスポンスに `X-Worker-Version` ヘッダーを付与します。**成功 (200) だけでなくエラーレスポンス (4xx / 5xx) にも付きます** — 古いクライアントが 404 でフィーチャー検出するケースでも version を学習できるように。

新規エンドポイント `GET /api/version`：

```json
{
  "version": "lazy-v2.11.0",
  "api_compatible_versions": ["lazy-v2.11.0"],
  "server_time_utc": "2026-05-10T03:14:15.123456Z"
}
```

`api_compatible_versions` は **Phase 1 では informational** です。将来的に「`2.11.x` クライアントは `2.11.0` worker と silent 互換」のような silent-set 拡張のためにワイヤフォーマットを予約しています。Phase 1 のクライアントは完全一致以外すべて警告します。

### クライアント側

`mcp_async_call.py` と `job_queue/client.py` は worker への各 HTTP リクエスト直後に共通ヘルパー `_check_worker_version(resp)` を呼びます。共通ヘルパーは `job_queue/versioning.py` に集約されているので、両者が独立に drift する心配はありません。

警告メッセージ例：

```
[mcp-async-skill] WARNING: worker version lazy-v2.10.1 != client version lazy-v2.11.0. The worker process may be stale (e.g. you upgraded the skill but the previous worker daemon is still running).
  Fix: curl -X POST http://127.0.0.1:54321/api/worker/shutdown
  Then re-run the client; a fresh worker will spawn at the new version.
```

`X-Worker-Version` ヘッダーが **欠けている** ケース（lazy-v2.10.x 以前の worker は付けてくれません）の警告：

```
[mcp-async-skill] WARNING: worker did not advertise X-Worker-Version. You may be running a pre-v2.11.0 worker against a v2.11.0 client.
  Fix: curl -X POST http://127.0.0.1:54321/api/worker/shutdown
  Then re-run the client; a fresh worker will spawn at the new version.
```

### 警告は 1 プロセスにつき 1 回だけ

警告は `versioning.py` のモジュールレベルガードで **1 プロセスにつき 1 回だけ** 出ます。同じ Python セッションで多数のリクエストを投げても stderr が埋まりません。pytest や CI 出力に微量混入する可能性はありますが、ノイズは限定的です。

logging ではなく `print(..., file=sys.stderr)` を使っています。エンドユーザーの CLI セッションは通常ログハンドラを設定していないため、確実に視認できることを優先しました。

## アップグレード手順

新版インストール前に、以下のいずれかの方法で古い worker を停止してください。

### 推奨: HTTP API で graceful shutdown

#### bash (Linux / macOS / WSL / Git Bash)

```bash
# 古い worker を止める
curl -X POST http://127.0.0.1:54321/api/worker/shutdown

# 新版を上書き展開
mkdir -p .claude/skills
curl -fSL -o mcp-async-skill.tar.gz \
  https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/latest/download/mcp-async-skill.tar.gz
tar xzf mcp-async-skill.tar.gz -C .claude/skills/
rm mcp-async-skill.tar.gz

# 次の CLI 呼び出しで新 worker が自動起動します
```

#### PowerShell (Windows)

```powershell
# 古い worker を止める
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:54321/api/worker/shutdown"

# 新版を上書き展開
New-Item -ItemType Directory -Force -Path .claude\skills | Out-Null
curl.exe -fSL -o mcp-async-skill.tar.gz `
  https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/latest/download/mcp-async-skill.tar.gz
tar xzf mcp-async-skill.tar.gz -C .claude\skills\
Remove-Item mcp-async-skill.tar.gz

# 次の CLI 呼び出しで新 worker が自動起動します
```

### 代替: idle timeout を待つ

`queue_config.json` の `idle_timeout_seconds` (既定 60 秒) を超えてアイドル状態になった worker は自動終了します。新規ジョブが流れていない時間帯ならば、何もせず待つだけで OK です。

### 警告が出てしまった場合

stderr に「worker version mismatch」が出た時点で、上の `curl -X POST .../api/worker/shutdown` を実行 → CLI を再実行すれば、新 worker が自動起動して警告も消えます。**警告が出てもジョブの処理自体は続行されます**（要件・既存挙動を壊さないため）。

## バージョン文字列の出処

`__version__` は `.claude/skills/mcp-async-skill/scripts/job_queue/__init__.py` に定義されています。

- ソースの既定値: `"0+dev"` (リリースされていない main / 開発作業ツリー用)
- リリースタグから tarball を作る GitHub Actions ワークフローが、tarball 生成前に `__version__` を `lazy-vX.Y.Z` に書き換えます。
- 書き換え失敗を検知するため、ワークフローは `grep -q` による pre/post 検証を行い、`__version__` 行が存在しない / 期待形式に書き変わらない場合は CI を fail します。
- 同じワークフローが main にも書き戻すので、`git clone` 派の利用者にも同じ version が見えます。

## 設計判断

- **警告のみ、failure ではない**: skew 中でも worker が応答する API パス自体は動くケースが多く、警告だけで先に進める設計です。「設定したのに効かない」を ユーザーが気付ける形にすることが目的で、ジョブを止めることが目的ではありません。
- **`logging` ではなく `stderr print`**: CLI ユーザーは通常ログハンドラを設定しないため、確実に表示することを優先しました。
- **process-local one-shot guard**: 同じ Python セッション内で複数回呼んでも 1 回しか出ません。`reset_warned_for_tests()` はテスト専用のリセット API です。
- **`api_compatible_versions` は Phase 1 では informational**: 将来 silent-set を広げる余地を残すためにフィールドだけ予約しています。Phase 1 のクライアントは完全一致以外すべて警告します。
- **ヘッダーは success/error 関係なくすべての JSON レスポンスに付与**: 古いダッシュボードや CLI が 404 でフィーチャー検出するパスでも version を学習できるよう、`_send_json()` 経由のレスポンスは status_code 不問でヘッダーを付けます。
