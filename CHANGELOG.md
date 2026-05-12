# Changelog

## [Unreleased] — targeting lazy-v2.13.0

### Changed (BREAKING)
- **`GET /api/jobs/<id>` の `result` フィールドが、有効な JSON object/array なら parse 済み object として返るようになります** ([#73](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/issues/73)): これまで `result` は DB に格納された JSON 文字列をそのまま返していました (`args` だけは parse 済み object として返る、という非対称な API)。lazy-v2.13.0 から `result` も parse 済み object として返り、加えて kamui-code MCP の `remote_result.content[].text` (こちらもさらに JSON 文字列としてエンコードされている) には parse 済みの `text_parsed` フィールドを併記します。
  - **影響**: `result` を文字列として参照 (`jq -r '.result'` 等) していた外部スクリプトは動作が変わります。本リポジトリ内部 (Python クライアント / CLI / dashboard) の影響箇所は調査・修正済み。
  - **新形状の例**:
    ```jsonc
    {
      "result": {                                  // ← object (was string)
        "remote_result": {
          "content": [{
            "type": "text",
            "text": "{\"video\":{\"url\":\"https://...\"}}",  // 原文を保持
            "text_parsed": {                                  // ← 新規
              "video": {"url": "https://..."}
            }
          }]
        },
        "local_files": ["C:\\Users\\..\\out.mp4"],
        "download_errors": []
      }
    }
    ```
  - **`result` の正確な変換ルール**:
    - **valid な JSON object / array string**: parse 済み object として返る (= 上の例)
    - **`null` / `""` (空文字列)**: そのまま (None / 空文字) で返る — 「ジョブ未完了」「結果なし」を保持
    - **不正 JSON 文字列 / prose / scalar JSON (`"42"`, `"true"` 等)**: string のまま fallback で返る — kamui-code 以外の executor が unstructured text を保存していた場合の互換のため
    - **既に parse 済みの dict が DB に入っていた場合 (将来の DB 層変更への備え)**: そのまま object として通り、`remote_result.content[].text` のアノテーションも適用される
  - **`text_parsed` の付与ルール**:
    - `text` が `{` または `[` で始まる string で、JSON として parse 可能なら付与
    - 不正 JSON / prose / 数値などは付与しない (= フィールド非存在)
    - **ネスト展開は 1 段のみ**: `text_parsed` の中にさらに JSON-as-string が含まれていても再展開しない (worker の責務範囲を有限に保つため)
    - **サイズ上限なし**: streaming response や inline base64 のように multi-MB の payload を返すエンドポイントを正しく扱うため。`json.loads` 自体は数 MB で十数 ms オーダーなので応答時間に問題は出ない
  - **`/api/jobs/<id>` の `args` の挙動は実質互換**: 旧 `json.loads(j["args"])` インライン処理を `_normalize_args_payload` ヘルパ経由に変更しましたが、内部実装は **同じく `json.loads` を直接呼ぶ** 形を維持。これにより legacy code が parse できていた **scalar JSON** (`"42"`, `"true"`, `"null"`, `"\"x\""`) も引き続き parse されて返ります。`args` 内の `content[].text` には `text_parsed` は **付与しません** — submitted args が round-trip で byte 一致することを期待する consumer (テスト・監査ログ等) を壊さないため。

### Added
- **worker.py に `_normalize_result_payload` / `_normalize_args_payload` / `_annotate_content_text` / `_try_json_loads` を追加** ([#73](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/issues/73)): kamui-code MCP のレスポンス形状の理解を worker 単独の責務に集約。dashboard やその他のクライアントが二重 JSON ロジックを実装しなくて済みます。
- 新規テスト: `test_worker_normalize.py` (31 tests) — `_try_json_loads` / `_annotate_content_text` / `_normalize_result_payload` / `_normalize_args_payload` の正常系・異常系を pin。サイズ上限なしを `test_parses_large_json_without_size_cap` (5 MB の valid JSON object) で明示。args の scalar JSON (`"42"`, `"true"`, `"null"` 等) 互換維持を `test_parses_scalar_json_for_legacy_compat` で pin。
- 新規 smoke test (2 件): `test_dashboard_js_skips_text_when_text_parsed_is_present` — walker が text_parsed の sibling text をスキップすることを pin。`test_dashboard_js_skip_guard_handles_text_parsed_null` — text_parsed が null や非 object scalar の場合は skip しないことを pin (text に URL が埋まったままのケースを想定)。
- **テスト fixture 中の URL は `https://example.com/...` を使用**: `test_worker_normalize.py` の二重 JSON テストでは「shape を pin したい」のが目的で、URL のドメイン値そのものは情報を持たない。下流 MCP サービスの実ドメインを test fixture に焼き付けない方針 (Issue [#76](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/issues/76))

### Changed
- **dashboard.js: `extractUrls` から二重 JSON parse 分岐を削除**: lazy-v2.13.0 worker が `text_parsed` を併記するようになったので、JavaScript 側で `JSON.parse(trimmed)` を呼ぶ必要がなくなりました。`(parsed text)` という path ラベルも消えます。これにより dashboard.js の純粋ロジックが縮小し、kamui-code 固有の応答形状に依存しないシンプルな再帰 walker になります。
- **dashboard.js: `showJobDetail` の `typeof === "string"` fallback も削除**: `result` は常に object 前提。万一旧 worker (≤ lazy-v2.12) と組み合わせて使われた場合、URL 表面化は劣化しますが ([PR #62](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/pull/65) の version handshake が「worker を再起動してください」と警告を出します)、raw JSON は引き続き `<details>` 内で参照できます。
- **`test_dashboard_smoke.py::TestJobDetailUrlSurfacing::test_dashboard_js_no_longer_double_parses_kamui_text`**: 旧 `(parsed text)` ラベル要求テストを「`JSON.parse(trimmed)` が dashboard.js に **存在しない**」逆向きの assertion に書き換え、再混入を防止。
- **dashboard.js: `text_parsed` を持つ dict node では sibling の `text` を walker でスキップ**: worker 側で text_parsed が併記されるようになったので、walker が text (生 JSON 文字列) と text_parsed (parsed object) の両方から URL を抽出すると重複が発生する。sibling の text_parsed を検知したら text の visit を skip することで、dedup ヘルパに頼らず根本で重複を防止。新規 smoke test `test_dashboard_js_skips_text_when_text_parsed_is_present` で pin。

### Compatibility
- **lazy-v2.12.x 以前の worker と lazy-v2.13.0 dashboard の組み合わせ**: 旧 worker は `result` を string、`text_parsed` なしで返します。新 dashboard は `result` が object でないと extractUrls が深く展開できないため、Outputs セクションが空 (もしくは prose URL 抽出だけ) になる可能性があります。raw JSON は引き続き表示可能。**worker と dashboard を同時にアップデートすることを推奨**します (元々 lazy-v2.11+ の worker version handshake が同じ意図で警告を出しています)。
- **lazy-v2.13.0 worker と lazy-v2.12.x 以前の dashboard の組み合わせ**: 旧 dashboard が `result` を string として `JSON.parse` する fallback ロジックを持っているため、object として返っても問題なく動作します (typeof チェックでスキップされる)。`text_parsed` フィールドは無視されますが、旧 dashboard 自身が二重 JSON 分岐を持つので URL は引き続き見えます。
- **外部スクリプト / 自前 CLI で `result` を文字列として `jq` 等で扱っているケース**: 動作変化があります。新形状を `jq -r '.result | tojson'` のように再シリアライズすれば従来通り扱えます。

## [lazy-v2.12.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.12.0) (2026-05-11)

### Added
- **Queue Dashboard: ジョブ詳細モーダルに Inputs / Outputs URL リンク化** ([#72](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/pull/72)):
  - 完了 / 失敗ジョブをクリックして開く詳細モーダルの上部に **Inputs (submit args の URL)** と **Outputs (result の URL)** セクションを追加。`args` と `result` の JSON を再帰 walk して `https?://...` 文字列を全部抽出し、JSON path (`result.images[0].url` など) と共に一覧表示。
  - **kamui-code MCP の二重 JSON エンコード対応**: kamui-code はレスポンスペイロードを `remote_result.content[].text` に *JSON 文字列として* 埋め込んで返します。walker は `{` / `[` で始まる文字列を try-parse して中も再帰し、path に `(parsed text)` ラベルを付けて出処を明示。これがないと完了ジョブの肝心な `images[].url` / `video.url` が表面に出ない。
  - **ローカル生成物パス (`local_files`) の表示**: result に含まれる Windows (`C:\...`) / POSIX (`/home/...`) 絶対パスを別カテゴリ `local` として表示。`file://` リンクは多くのブラウザで開けないため Copy ボタンのみ提供 (Explorer / Finder に貼り付けて開く前提)。
  - **画像 URL は thumbnail プレビュー** (lazy load) + Open / Copy ボタン。動画 / 音声 / その他は左ボーダーの色で kind を識別 (image=緑、video=紫、audio=黄、other=青)。
  - **Copy ボタン**: 1 クリックで URL / パスをクリップボードへ (`navigator.clipboard.writeText`)、視覚フィードバック (`✓` → 1.5 秒後に元に戻る)。
  - **モーダルメタヘッダー**: Job ID / Status pill (色付き) / Endpoint をモーダル先頭に表示。raw JSON 全文は `<details>` セクションに格納 (URL が見つかれば閉じた状態、見つからなければ開いた状態がデフォルト)。
  - 抽出は副作用なし、`maxDepth=14` / `maxNodes=5000` の上限で大きな result blob でも UI freeze しない。実 DB サンプル (t2i / t2v_sd2 / r2v_sd2) で動作検証済み — r2v_sd2 の `image_urls[0]` (参照画像 URL) も Inputs に表面化。
- 新規テスト: `test_dashboard_smoke.py::TestJobDetailUrlSurfacing` (5 tests) — index.html の `<div id="job-detail-content">` 化 / dashboard.js に `extractUrls` 関数定義 / `(parsed text)` 二重 JSON ラベル / `looksLikeLocalPath` 関数定義 / dashboard.css に `.url-section` + `.url-thumb` + 4 kind variant の存在を pin
- 全 `scripts/tests/` で 423 tests pass (lazy-v2.11.1 から +5)

## [lazy-v2.11.1](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.11.1) (2026-05-10)

### Fixed
- **`r2v` (reference-to-video) を独立カテゴリ化**: `r2v` は当初 `i2v` への alias として実装されていましたが、アップストリーム MCP サービスが `r2v` を `i2v` と独立にレートリミットしていることが確認されたため、独立カテゴリとして扱うよう修正しました。alias のままだと `r2v` ジョブが `i2v` 枠を消費し、`i2v` 側で予期しない 429 を誘発する可能性がありました。
  - `KNOWN_CATEGORIES` に `r2v` を追加 (`{t2i, i2i, t2v, i2v, r2v}`)
  - `DEFAULT_ALIASES` から `r2v` を削除 (`{r2i: i2i}` のみ残存)
  - **`FORBIDDEN_ALIASES` 機構**: ユーザー `queue_config.json` に `aliases.r2v` が残っていても、起動時に **強制的に削除** + 警告ログを 1 回出力。利用者の queue_config.json を直接書き換える必要なく、自動で正しい挙動になります。
  - `generate_skill.py` / `queue_config.example.json` の初期同梱 categories に `r2v` を追加 (`limits.r2v: {max_inflight: 1, min_interval: 1.0, exhaust_cooldown: 3600}`)
  - `mcp_async_call.py --pause-category` の help を `(t2i, i2i, t2v, i2v, r2v)` に更新
  - 詳細: [docs/category-limits.md](docs/category-limits.md) 「`r2v` の取り扱い (lazy-v2.11.1+)」

### Compatibility
- **lazy-v2.10.x / lazy-v2.11.0 の `queue_config.json` (`aliases: {"r2i": "i2i", "r2v": "i2v"}`) はそのまま動作します**。起動時に `[CategoryLimiter] (instance ...) ignored forbidden alias keys in config (\`aliases.r2v\`)` 警告が 1 回出力され、`r2v` URL は `r2v` 独立カテゴリとして集計されるようになります。
- 既存利用者は `queue_config.json` の `aliases` から `r2v` を削除し、`limits.r2v` を追加することを推奨します (動作は自動で正しくなりますが、設定ファイルが意図と一致するほうが望ましい)。
- **`r2i` (reference-to-image) は引き続き `i2i` の alias** として扱われます。`r2i` のアップストリーム独立レートリミットの有無は未確認のため、保守的にこの仕様を維持します。確定情報が入り次第、別 PR で対応予定です。

### Tests
- 新規テスト: `test_category_limiter.py` の `TestForbiddenAliases` クラス (3 tests) — `FORBIDDEN_ALIASES` 定数 / `DEFAULT_ALIASES` から r2v 除外 / ユーザー `aliases.r2v` の silent drop + 警告発火を検証
- 更新テスト: `test_extracts_r2v_as_independent_category` (旧 `test_aliases_r2v_to_i2v` を置き換え) / `TestKnownCategoriesConstant` を r2v 含む 5 カテゴリに更新 / `TestPublicCategoryAPI.test_get_categories_returns_sorted_deterministic` を 5 カテゴリ並び順に更新
- 全 `scripts/tests/` で 418 tests pass (lazy-v2.11.0 から +3)

## [lazy-v2.11.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.11.0) (2026-05-10)

### Added
- **Per-category individual rate limits** (#59): t2i / i2i / t2v / i2v ごとに `max_inflight` / `min_interval` / `exhaust_cooldown` を独立設定可能
  - 新スキーマ `category_rate_limits.limits.{cat}.{key}` (旧スキーマ `max_category_inflight` は後方互換ロード + 1 回 deprecation log、`lazy-v2.13.0 以降で削除予定`)
  - `PATCH /api/config` 新形式: `{"category": {"limits": {"t2v": {"max_inflight": 1}}}}`
  - 旧 `lazy-v2.10.x dashboard` 互換: 旧形式 PATCH (`{"category": {"max_inflight": N}}`) を全カテゴリ一括適用として受理 (`_legacy_warning` を applied[] に含む)
  - `GET /api/config` レスポンスに新キーと旧互換ミラーキー (`category.max_inflight` 等、最初のカテゴリの値) を併記
  - `category_limiter.py` の public API 拡張: `is_known_category()`, `get_categories()`, `get_max_inflight(cat)`, `get_min_interval(cat)`, `get_exhaust_cooldown(cat)`
  - 詳細: [docs/category-limits.md](docs/category-limits.md)
- 新規テスト: `test_category_limiter.py` (37 tests), `test_worker_config.py` (18 tests), 並行性 / 後方互換 / 入力検証 / unknown endpoint dispatch / legacy timestamp parse をカバー
- **Worker version handshake** (#62): 上書きインストールで古い worker daemon が残った場合の skew を検知して stderr に 1 回警告
  - 全 JSON レスポンス (200 / 4xx / 5xx すべて) に `X-Worker-Version` ヘッダー付与
  - 新規エンドポイント `GET /api/version` (`version` / `api_compatible_versions` / `server_time_utc`)
  - 新規モジュール `job_queue/versioning.py` に共通ヘルパー (`get_worker_version`, `warn_if_version_mismatch`, `reset_warned_for_tests`)。`mcp_async_call.py` と `job_queue/client.py` 両方が同じヘルパーを呼ぶ
  - クライアント側 check は worker への全 HTTP 呼び出しに統合: `_queue_wait` / `_queue_list` / `_queue_stats` / `--pause-category` / `--resume-category` / `submit_job` / `wait_job` / `is_worker_running`
  - 警告は **process-local one-shot guard** で 1 プロセスにつき 1 回だけ stderr に出る
  - `api_compatible_versions` は **Phase 1 では informational** (フィールドだけ予約、警告抑制はしない。将来の silent-set 拡張用)
  - `.github/workflows/release.yml` 強化: `__version__` stamp に pre/post `grep -q` 検証を追加。失敗を CI で止め、tarball が `0+dev` で出荷されて全クライアントが自己 mismatch 警告する事故を防止。同じ workflow が main にも書き戻すので git clone 派の利用者にも同じ version が見える
  - 詳細: [docs/version-handshake.md](docs/version-handshake.md)
- 新規テスト: `test_versioning.py` (13 tests), `test_worker_version.py` (11 tests), one-shot guard / 4xx / 5xx ヘッダー / `/api/version` レスポンス形状 / constant wiring / health-probe 統合をカバー
- **DB schema migration foundation** (#63): `PRAGMA user_version` ベースのマイグレーションフレームワークを導入 (Phase 1 では足場のみ、実カラム変更は将来 migration で対応)
  - 新規モジュール `job_queue/migrations.py` に `MIGRATIONS` レジストリ (`Callable[[sqlite3.Connection], None]` 型注釈付き) と driver `apply_migrations()`
  - `JobStore.__init__` を `_init_schema_and_migrations()` に改名し、(1) `CREATE TABLE IF NOT EXISTS` → (2) `apply_migrations` の順序固定で実行
  - migration 001 (`migrate_001_initial`) は **required column NAMES のみ検証** (型・NOT NULL・DEFAULT は見ない、PHASE1_PLAN_v3 fix #7)
  - 必須カラム欠損 → `RuntimeError` で停止 (自動修復しない、ユーザーに DB 退避を促す)
  - 余分なカラム → warning ログのみで起動続行 (forward-compat: 新版 worker が追加したカラムを旧版が見るケース)
  - migration 失敗時は connection を close して例外を re-raise (Windows 環境でファイルハンドル即解放)
  - 詳細: [docs/db-migration.md](docs/db-migration.md)
- 新規テスト: `test_db_migrations.py` (12 tests), 新規 DB / pre-PR3 DB upgrade / missing column rejection / extra columns warn / type drift acceptance / registry shape / driver behaviour をカバー
- **Custom Groups (endpoint-pattern rate limiting)** (#60): endpoint URL の glob パターンで独自グループを定義し、カテゴリと独立に inflight / min_interval / 429 cooldown を制御
  - 新スキーマ `custom_groups.{name}` (`endpoints` / `max_inflight` / `min_interval` / `exhaust_cooldown`)
  - **マッチした endpoint はカテゴリ計上から完全除外** — 群を厳しく絞ってもカテゴリ全体には影響しない
  - First-match-wins 順序 (Python 3.7+ の dict 挿入順保証)、glob は fnmatch ベース case-sensitive
  - 新規モジュール `job_queue/custom_group_limiter.py` + 共通 `job_queue/limiter_state.py` (`LimiterStateMixin`)。CategoryLimiter も同 mixin に再構成し、状態管理を 1 箇所に集約 (バグ修正・拡張が一度で済む)
  - 新規 API: `GET /api/groups`, `POST /api/groups/{name}/{pause|resume}` (404 with `available_groups`), `PATCH /api/config` の `groups` block 受理 (per-category と同じ validation pattern)
  - `GET /api/config` / `GET /api/stats` レスポンスに `custom_groups` セクション追加
  - `POST /api/jobs` の pause warning が resolved limiter (group/category) を反映
  - dispatcher は新ヘルパー `_resolve_limiter(endpoint) -> (limiter, key)` 1 箇所経由で limiter にアクセス。group → category → `(None, None)` の優先順位、unknown endpoint は per-key accounting なしで dispatch (PR1 `TestUnknownCategoryEndpointDispatch` 契約維持)
  - **Bytedance Seedance v2.0 動画モデル群 (`t2v_sd2` / `i2v_sd2` / `r2v_sd2`) を初期同梱** (`generate_skill.py` テンプレート + `queue_config.example.json`)。URL prefix が標準カテゴリ list 外のため、未設定だと rate-limit accounting がスキップされる問題への安全策
  - 詳細: [docs/custom-groups.md](docs/custom-groups.md)
- 新規テスト: `test_custom_group_limiter.py` (38 tests), `test_dispatcher_groups.py` (12 tests), `test_worker_groups.py` (16 tests), `test_generate_skill_queue.py` の Seedance pin (3 tests) — 構築 / glob / concurrent smoke / unknown handling / dispatcher routing isolation / HTTP API / template defaults をカバー
- **Queue Dashboard: per-category UI + Custom Groups + graceful degrade + `--port 0`** (#61, #53):
  - **Categories と Custom Groups の二段組レイアウト**: 左に t2i/i2i/t2v/i2v、右にユーザー定義グループ。900px 以下では縦積みに自動切替。グループカードは indigo 左ボーダーで識別性向上、マッチする endpoint パターンをカード内に inline 表示
  - **Per-category Settings グリッド**: t2i/i2i/t2v/i2v 各行に `max_inflight` / `min_interval` / `429 cooldown` の入力欄。空欄 skip ロジックで「変えたいフィールドだけ送信」(`0` は有効値として通過)
  - **Per-group Settings グリッド**: 各カスタムグループに 1 行ずつ。Group 名 tooltip でマッチ endpoint パターン確認
  - **Worker version 表示** (PR2 連携): ヘッダーに `/api/version` 経由でバージョン表示。古い worker なら `(pre-v2.11.0)`
  - **Compatibility banner (graceful degrade)** (PHASE1_PLAN_v3 fix #2): `cfg.category.limits` 不在を検出して Settings パネルに warning + 復旧手順 (`curl -X POST .../api/worker/shutdown`) 表示。jobs list / stats は引き続き動作
  - **新 API endpoints の whitelist 追加**: `/api/version`, `/api/groups`, `POST /api/groups/{name}/{pause|resume}` (`{name}` は `[A-Za-z0-9_\-.]+` を許容)
  - **`--port 0` 動的ポート割当** (#53): `--port 0` で OS が空きポートを選択、stdout に `PORT=NNNNN` 形式で実ポート出力。subprocess driver で grep parse 可能。54322 が他サービスと衝突する long-standing pain point の解消
- 新規テスト: `test_dashboard_smoke.py` (7 tests) — `--port 0` の subprocess + `PORT=NNNNN` parse + 実 HTTP 疎通、proxy whitelist (`/api/version` / `/api/groups` / `POST /api/groups/{name}/{action}` / dotted group name / unauthorized POST blocked) をカバー。Windows 環境対応で `PYTHONIOENCODING=utf-8` を Popen に強制

### Changed (BREAKING)
- **`set_max_inflight(cat, value)` の `cat` 引数必須化**: lazy-v2.10.x の単一引数 `set_max_inflight(value)` (全カテゴリ一括変更) は削除。`set_min_interval` / `set_exhaust_cooldown` も同様。worker の `PATCH /api/config` ハンドラが「全カテゴリ一括」の API レイヤを emulate する。
- **`acquire_inflight("unknown") == can_submit("unknown") == False` に統一**: lazy-v2.10.x では unknown でハードコード default の inflight state を作っていたが、`can_submit` と整合しない不整合だった。現在は unknown は category accounting 対象外。dispatcher は `extract_category() is None` のとき category check をスキップする経路を従来から持っている。
- `dispatcher.py` の `category_limiter._exhaust_cooldown` 直接参照を `get_exhaust_cooldown(category)` 公開 API 経由に変更 (per-category 化への移行で private 直触りが壊れるため)。

### Fixed
- **`db.py` purge_old_jobs / get_stale_polling のタイムスタンプ精度バグ** (PR #45 由来の silent failure):
  - `95ebebb` で `update_status` が `_utc_now_iso()` (μs 精度) に切り替わったが、`purge_old_jobs` と `get_stale_polling` は SQL の `julianday('now')` (秒精度) を使い続けていた。Python 側のタイムスタンプが SQL の `now` より数百 μs 未来になり、経過時間が負になって `retention_seconds=0` でも 0 件削除になっていた。
  - 修正: `_utc_now_iso()` をバインドパラメータとして渡し、両辺で同じ Python wall-clock を使用。
- **`test_dispatcher_rate_limit.py` の 429 期待値** (PR #47 で意図的に変更されたが test 未更新):
  - `Retry-After` ヘッダー無視 (kamuicode MCP は信頼できる Retry-After を返さない)
  - per-category cooldown 適用 (デフォルト 3600s)
  - 一律 `pending` に再キュー (remote_job_id があっても recovering にしない)
  - テストを現仕様に合わせて書き直し、PR #47 の判断理由を docstring に明記
- **`test_executor.py` の recovery session 期待値** (PR #47 SessionManager 導入で変更されたが test 未更新):
  - kamuicode MCP の中継 session は短命、fal.ai の remote_job_id は長命というアーキテクチャに基づき、recovery 時は古い session_id を捨てて fresh middleware session で remote_job_id を polling する
  - テストを「fresh session + remote_job_id 経由 polling」期待に書き直し

### Compatibility
- **lazy-v2.10.x の queue_config.json (旧 flat schema) は引き続き動作**します (起動時に `[CategoryLimiter] (instance ...) DEPRECATED: ...` 警告ログが 1 回出力)
- **lazy-v2.10.x dashboard は引き続き動作**します (`GET /api/config` の旧互換ミラーキーで UI 値が空にならない)。ただし旧 dashboard から値編集すると **新 worker は全カテゴリに一括適用** します。per-category 編集には `lazy-v2.11.0` 以降の dashboard (本リリース同梱) が必要。
- **lazy-v2.10.x worker と lazy-v2.11.0 client の組み合わせ**: 新クライアントは旧 worker から `X-Worker-Version` が返らないことを検知し、stderr に「pre-v2.11.0 worker の可能性、shutdown して再起動を」と 1 回警告します。処理は止めません (skew 中でも動く API パスがあるため)。**新版を上書きインストールする前に `curl -X POST http://127.0.0.1:54321/api/worker/shutdown` で古い worker を停止することを推奨** (詳細: [docs/version-handshake.md](docs/version-handshake.md))。
- **`custom_groups` キーが無い既存 `queue_config.json`**: 完全互換。`custom_groups` は空 dict 扱いとなり、すべての endpoint がカテゴリ経由でルーティングされます (lazy-v2.10.x と同じ挙動)。新規インストールでは Seedance v2.0 動画モデル群 (`t2v_sd2` / `i2v_sd2` / `r2v_sd2`) が初期同梱されます — 既存ユーザーが Seedance v2.0 モデルを使う場合は `queue_config.json` に手動で追加するか、新規スキル生成 (`generate_skill.py`) を流すと反映されます。
- **lazy-v2.10.x dashboard との組み合わせ**: dashboard は `custom_groups` セクションを表示しませんが、worker の HTTP API は引き続き動作します。`/api/config` の `custom_groups` フィールドは旧 dashboard には無視されるため、`category_rate_limits` の旧互換ミラーキーと同じく "ignore unknown fields" 戦略で安全に共存します。
- **lazy-v2.11.0 dashboard と pre-v2.11 worker の組み合わせ**: 新 dashboard は `cfg.category.limits` 不在 + `/api/version` 404 を検出すると Settings パネルに **graceful degrade banner** を表示し、復旧手順 (`curl -X POST .../api/worker/shutdown`) を案内します。jobs list / stats / endpoint table は引き続き機能するので「ダッシュボードが完全に死ぬ」状態にはなりません。
- **`--port 0` で実ポートを別プロセスから知りたい場合**: stdout の最初の出力が `PORT=NNNNN` 形式 (machine-parseable) になっています。`grep -oP 'PORT=\K\d+'` (bash) や `Select-String` (PowerShell) で取得できます。

### Repository (history rewrite)
- **2026-05-10: `git filter-repo` による history rewrite を実施** ([Issue #69](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/issues/69))
  - 削除対象: 誤って commit されていた生成済みスキル群 `.claude/skills/{t2i,i2i,t2v,i2v,r2i,r2v,file-upload}-*` (92 ディレクトリ / 1,104 ファイル / 約 16MB)
  - 影響: `main` および `lazy-v*` 全 23 tag の commit hash が変化 (うち `lazy-v2.0.0`〜`lazy-v2.2.1` の 8 tag は tree 内容は同じだが filter-repo の標準挙動として commit object が再生成され hash 変化)
  - 残骸防止: `.gitignore` に generated skills パターン追加 + `.github/workflows/no-generated-skills.yml` で CI guard
  - **既存 clone を持っている方は再 clone または `git reset --hard origin/main` が必要** (詳細手順は README の「⚠️ 既存 clone を持っている方への重要なお知らせ」を参照)
  - 5 個の merged feat/* branch (`feat/custom-groups`, `feat/dashboard-groups`, `feat/db-migration-foundation`, `feat/per-category-limits`, `feat/worker-version-handshake`) は削除済み (内容はすべて main に統合済み)
  - 既知の制限: GitHub 内部の `refs/pull/64〜68/head` には rewrite 前の tip commit が残存。通常 clone からは到達不可、明示 fetch でのみ visible (詳細: Issue #69 クロージングコメント)

## [lazy-v2.10.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.10.0) (2026-04-23)

### Fixed
- **Stale polling検出** (#55): pollingスレッドが死んでもジョブが永久放置される問題を修正
  - dispatch_once()にPhase 3追加: status=pollingかつupdated_atが30分以上前のジョブをrecoveringに自動降格
  - _poll_and_get_result()にheartbeat追加: 5分ごとにupdated_atを更新してstale誤判定を防止
  - stale_polling_timeout_secondsをqueue_config.jsonで設定可能（デフォルト1800秒=30分）
- **Pause粒度の細分化** (#56): 非429エラー時のpauseをカテゴリ全体→エンドポイント単位に変更
  - 1エンドポイントのバリデーションエラーで同カテゴリ全体が停止する問題を解消
  - dispatcherに`_endpoint_paused`/`_endpoint_pause_reason`を追加
  - `POST /api/endpoints/resume`でエンドポイント個別のresume
  - `/api/stats`にendpoint_pausesを追加
  - カテゴリpauseは手動操作用に残存

## [lazy-v2.9.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.9.0) (2026-04-21)

### Added
- **ダッシュボード設定パネル** — ☰ハンバーガーメニューからスライドイン設定パネル (#54)
  - カテゴリ設定: max_inflight / min_interval / exhaust_cooldown のリアルタイム変更
  - エンドポイント設定: default_max_concurrent / default_min_interval のリアルタイム変更
  - ワーカー設定: idle_timeout のリアルタイム変更
  - PATCH /api/config でバリデーション付き部分更新
- **登録済みスキル一覧** — GET /api/skills でスキルメタデータを走査・キャッシュ表示
- **ワーカー管理** — Start/Stop/Restartボタン
  - POST /api/worker/shutdown (202 Accepted + graceful停止)
  - GET /api/worker/status (PID, running状態, ジョブ数)
  - POST /api/worker/start, /api/worker/restart
- CategoryLimiter にruntime設定セッター追加
- QueueConfig に set_defaults() セッター追加
- worker.py に do_PATCH + GET /api/config 追加

## [lazy-v2.8.2](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.8.2) (2026-04-12)

### Fixed
- queue-dashboard: モーダルオーバーレイがページ読み込み時に常時表示される問題を修正 (#51)
  - CSSの `display:flex` が HTML `hidden` 属性を上書きしていた

## [lazy-v2.8.1](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.8.1) (2026-04-11)

### Docs
- README.md のセットアップ文言を更新（`mcp-async-skill` + `queue-dashboard` 同梱を明記）
- docs/release-process.md のビルド説明・展開テスト・ローカルビルド手順を v2.8.0 の2スキル同梱に合わせて更新

## [lazy-v2.8.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.8.0) (2026-04-11)

### Added
- **Queue Dashboard** — 新規独立スキル `queue-dashboard` (#50)
  - ブラウザから見えるキュー可視化Web UI（Python stdlibのみ、Vanilla JS）
  - `python .claude/skills/queue-dashboard/scripts/queue_dashboard.py` で起動 → `http://127.0.0.1:54322/` 自動オープン
  - サマリー（pending/running/completed/failed）、カテゴリ状態、エンドポイント統計、最近のジョブ一覧
  - カテゴリ pause/resume ボタン、ジョブクリックで詳細モーダル
  - **セキュリティ**: 許可パスホワイトリスト、1 MiBリクエスト上限、10秒タイムアウト、127.0.0.1 バインド既定
  - **UX**: 2秒ポーリング、エラー時指数バックオフ、非表示タブ時の間引き、Escape/背景クリックでモーダル閉
  - 同一originプロキシで `/api/*` を既存ワーカー(54321)へ中継 → CORS不要
- `release.yml` に queue-dashboard を同梱する配布ステップを追加

## [lazy-v2.7.1](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.7.1) (2026-04-11)

### Added
- **タイムゾーン明示化**: DBタイムスタンプにZサフィックスを付与しUTCを明示
  - `insert_job` / `update_status` がPython側で `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")` を生成
  - `/api/stats`, `/api/jobs`, `/api/jobs/{id}`, `/api/categories` レスポンスに `server_time_utc` を追加
  - 各ジョブに `created_age_seconds` / `updated_age_seconds` を追加（LLMが相対時間を正しく判断できるように）
- 既存DBのZなしレコードもUTCとしてパースするフォールバック処理
- SKILL.mdにタイムゾーン取り扱いセクション追加

### Background
- kamuicode MCPサーバーのレスポンスはUTC
- LLMの `currentDate` はユーザーローカル時刻（JST等）の場合があり、DBタイムスタンプとの比較で誤判断が起こりうる
- `age_seconds` を使えばタイムゾーン非依存で相対時間を判断できる

## [lazy-v2.7.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.7.0) (2026-04-10)

### Added
- **Codex CLI対応** (#49): `.agents/skills/` への生成をサポート
  - `generate_skill.py --codex` フラグで `.agents/skills/` にスキル生成
  - `find_project_root()` が `.agents/` ディレクトリも探索
  - キューディレクトリ探索（`.claude/queue/` / `.agents/queue/`）の両対応
  - `_resolve_db_path()` が両パスを探索
  - SKILL.md にClaude Code / Codex CLI両方の使用方法を記載
  - README.md にCodex CLIインストール手順を追加

## [lazy-v2.6.3](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.6.3) (2026-04-10)

### Fixed
- 429 HTTPErrorが `status_code=0` で非429扱いされカテゴリ全体がpauseする問題を修正 (#48)
  - `.response` 属性が取得できない場合、例外メッセージからステータスコードをフォールバック抽出
- 同カテゴリ内の複数エンドポイントでsubmitが同時集中する問題を修正
  - dispatch_once()の1ラウンドでカテゴリあたり最大1ジョブのみdispatch

### Added
- CHANGELOG.md 作成（lazy-v2.0.0〜の全リリース履歴）
- README.md にカテゴリ制御・SessionManager・エラーハンドリング・pause/resume CLIの情報を反映

## [lazy-v2.6.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.6.0) (2026-04-08)

### Breaking Changes
- 仮想レートリミット（hourly/dailyカウント）を完全削除
- 429エラー時の挙動を変更: failed → pendingに戻して自動リトライ
- 非429エラー時の挙動を変更: カテゴリを即座にpauseし被害拡大を防止

### Added
- 非429エラー（422, 503等）でカテゴリ即pause + pause理由の詳細保持
- pause理由表示（`--stats`、ジョブ投入時の警告）
- `--resume-category` 時にcooldownもクリアし即座にdispatch再開
- 同カテゴリ内の複数エンドポイントでsubmitが同時集中する問題の修正

### Changed
- 429エラー → pendingに戻す + 1時間cooldown（自動回復）
- `auto_pause_after_consecutive_429` のデフォルトを 3 → 25 に変更
- `record_submit()` → `touch_submit()`（タイムスタンプのみ、カウントなし）

## [lazy-v2.5.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.5.0) (2026-04-08)

### Added
- **SessionManager**: endpoint+auth-context単位のMCPセッションキャッシュ
  - single-flight再initialize（Lock + Condition + 世代管理）
  - MCP仕様準拠: HTTP 404でセッション切れ検知 → 再initialize → 1回リトライ
  - recovery時は古いDB session_idを捨て最新セッションを使用
- **inflight制御**: カテゴリ別のsubmit同時実行数制御（デフォルト: 1）
- **ローリング窓cooldown**: force_exhaustからの経過時間でcooldown管理（デフォルト: 1時間）
- **連続429自動pause**: N回連続429でカテゴリを自動pause（手動resumeまで停止）
- **429/503区別**: 503は連続カウンタに加算しない、自動pause対象外

### Changed
- 固定ウィンドウ（clock-hour/calendar-day）リセットからローリング窓に変更

## [lazy-v2.4.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.4.0) (2026-04-07)

### Added
- **CategoryLimiter**: カテゴリ別（t2i/i2i/t2v/i2v）の仮想レートリミット
  - 固定ウィンドウ方式（hourly/daily）
  - r2i → i2i、r2v → i2v のエイリアス処理
- **カテゴリ手動pause/resume**: `POST /api/categories/{cat}/pause`, `resume`
- **CLI**: `--pause-category`, `--resume-category` オプション
- `/api/stats` にカテゴリ別使用状況を追加
- `queue_config.json` に `category_rate_limits` 設定を追加

### Changed
- submitのリトライ廃止（枠消費防止）。status/resultのリトライは維持
- 本物429検出時: failed + カテゴリexhausted（仮想リミットと区別した明確なエラーメッセージ）
- 非429エラーも構造化JSON（status_code, response_body）で詳細記録

### Fixed
- release tarballに `category_limiter.py` が欠落していた問題を修正
- ビルドワークフローを `job_queue/*.py` ワイルドカードコピーに変更

## [lazy-v2.3.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.3.0) (2026-04-07)

### Fixed
- **ディスパッチャスレッドのSQLite同時アクセスによるサイレント死** (#45)
  - `dispatcher._loop()` に例外ハンドリング追加（スレッド死防止 + 1秒バックオフ）
  - `worker._idle_monitor()` に例外ハンドリング追加（リソースリーク防止）
  - `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000` 設定
  - `JobStore` 全メソッドに `threading.Lock` ガード追加

## [lazy-v2.2.1](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.2.1) (2026-02-25)

### Changed
- `--header` CLIオプションを廃止し、`--config` からヘッダーを自動解決する方式に変更

## [lazy-v2.2.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.2.0) (2026-02-24)

### Changed
- 直接実行モードを完全廃止し、キューモードをデフォルトに統一

## [lazy-v2.1.2](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.1.2) (2026-02-23)

### Fixed
- デフォルトの `min_interval_seconds` を 2.0 → 10.0 に変更
- 429レートリミット時にジョブをpendingに戻しエンドポイントを一時停止する

## [lazy-v2.1.1](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.1.1) (2026-02-23)

### Added
- リリース時にドキュメントのダウンロードURLを自動更新するワークフロー

### Fixed
- release workflowで main ブランチを fetch してから checkout する

## [lazy-v2.1.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.1.0) (2026-02-23)

### Changed
- `max_polls` デフォルトを 300 → 3000 に10倍化し、定数として一元管理
- `.claude/queue/` を `.gitignore` に追加

## [lazy-v2.0.0](https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/tag/lazy-v2.0.0) (2026-02-22)

### Added
- curl + GitHub Releases によるインストール方式
- `--show-args`, `--filter-status` オプション
- SQLiteフォールバック（ワーカー停止時の読み取り専用操作）
- ワーカー自動起動
- 認証キーのハードコード除去（環境変数 / `.env` ファイル対応）

### Changed
- pip install 方式を廃止し、curl + tar.gz 方式に変更
- デフォルト出力先を常にCWDベースに変更

### Fixed
- タグプレフィックスを `v*` から `lazy-v*` に変更
