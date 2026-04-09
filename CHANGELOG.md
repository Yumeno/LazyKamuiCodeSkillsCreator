# Changelog

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
