# DB Schema Migration Framework

`lazy-v2.11.0` 以降、queue-system の SQLite DB に **`PRAGMA user_version` ベースの簡易マイグレーション機構** を導入しました。これは「将来 DB スキーマを変更したくなったときの足場」であり、PR3 (`#63`) 自体はカラムを追加・削除しません。

## なぜ必要か

### これまでの状況（lazy-v2.10.x まで）

`db.py` の `_init_schema()` は `CREATE TABLE IF NOT EXISTS jobs(...)` を実行するだけでした。新規 DB と既存 DB で同じ DDL が走り、既に存在するテーブルは何も変更されません。

このアプローチは **「カラム名・型・制約が初期コミットから変わっていない」期間は問題ありません**。実際これまで lazy-v2.10.x まで運用できていたのは偶然そうだったからです。

### 将来必ず壊れるシナリオ

```python
# 例: 将来 PR で `priority` カラムを追加したくなったとする
self.conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        ...
        priority INTEGER DEFAULT 0  # ← 新規追加
    )
""")
```

ユーザーが新版を **既存 DB に対して** 上書きインストールすると：

1. `CREATE TABLE IF NOT EXISTS` は既存テーブルを変更しない
2. でも新コードは `INSERT INTO jobs(..., priority) VALUES(...)` を発行
3. → `OperationalError: table jobs has no column named priority`
4. ユーザーは DB ファイルを手動削除しないと復旧できない（ジョブ履歴ロスト）

### Phase 1 の対応

PR3 はこの状況を防ぐため、**マイグレーションフレームワークだけ** を `lazy-v2.11.0` に組み込みます。実カラム追加は別 issue で扱い、その時点ではこの足場の上に migration 002, 003, ... を追加するだけで済みます。

## 仕組み

### ライフサイクル契約

`JobStore` の `_init_schema_and_migrations()` は順序固定で 2 ステップ実行します：

1. **`CREATE TABLE IF NOT EXISTS jobs (...)`**
   - 新規 DB と pre-PR3 DB の両方でテーブル存在を保証
2. **`apply_migrations(self.conn)`**
   - `PRAGMA user_version` を読み、ターゲットより小さければ migration 関数を順に実行
   - 各 migration 完了後に `PRAGMA user_version = N` を bump

migration 001 は **テーブルが存在することを前提** にしています。順序を逆にすると「テーブルが無いのに `PRAGMA table_info(jobs)` を見る」状態になり、誤って "schema drift" を報告します。順序保証は `db.py` 側のドキュメントと migration 001 側の docstring の両方に明記されています。

### マイグレーションの登録

`migrations.py`:

```python
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, migrate_001_initial),
    # 将来追加例:
    # (2, migrate_002_add_priority),
    # (3, migrate_003_add_user_id),
]
```

`Callable` 型注釈で signature drift を静的に検知できます。

### Migration 001 (`migrate_001_initial`) の役割

**やること:**
- `PRAGMA table_info(jobs)` で実際のカラム名集合を取得
- `V1_JOBS_COLUMNS` （Python 側の `frozenset`、**lazy-v2.11.0 ベースラインで凍結**）と差分を取る
- 必須カラムが欠けていれば `RuntimeError` で停止
- 余分なカラムがあれば warning ログだけ出して続行（forward-compat）
- 最後に `PRAGMA user_version = 1` を bump（apply_migrations 側で）

**やらないこと:**
- カラムの **型** チェック（`TEXT` vs `INTEGER` など）
- `NOT NULL` 制約の検証
- `DEFAULT` 句の検証

これは Phase 1 の意図的なスコープです。**「required columns exist」だけ確認** することで、SQLite の type-affinity 由来の些細な差異で既存 DB を弾くリスクを避けています。将来の migration が必要なら型チェックを追加してよいですが、migration 001 自体は forward-compatible に保ちます。

### ⚠️ `V1_JOBS_COLUMNS` は永久凍結

`V1_JOBS_COLUMNS` は **lazy-v2.11.0 時点の jobs テーブル必須カラム集合** です。**将来 migration がカラムを追加しても、この集合は絶対に更新しないでください**。

理由は「直接アップグレードパスの破綻」です。例えば:

1. PR3 (現在): `V1_JOBS_COLUMNS = {id, endpoint, ...}` (14 列)
2. 将来 PR: migration 002 で `priority` カラムを追加。**もし `V1_JOBS_COLUMNS` も更新してしまうと**...
3. lazy-v2.10.x ユーザーが lazy-v2.12.0+ に直接アップグレード
4. `user_version=0` の DB に対して migration 001 が走る
5. migration 001 が「`priority` が無い」と判定して `RuntimeError`
6. → migration 002 に到達できず **直接アップグレードが破綻**

`V1_JOBS_COLUMNS` を凍結することで、migration 001 は常に「v1 ベースラインの必須カラム」を検証し、その後 migration 002 / 003 / ... が順に列を追加していくチェーンが成立します。

## エラー時の挙動

### 必須カラムが欠けている場合

```
RuntimeError: [migrations] DB schema drift detected: jobs table is missing
required columns ['error', 'remote_job_id']. This DB was likely created by an
unsupported version. Back up the DB, move it aside, then restart the worker
to create a fresh DB.
```

このとき `JobStore.__init__` 内で connection を `close()` してから例外を re-raise するので、Windows でもファイルハンドルは速やかに解放されます。ユーザーは DB ファイルを退避してから worker を再起動すれば、新規 DB が `user_version=1` で作成されます。

**意図的に自動修復しません**: 欠損カラムを `ALTER TABLE ADD COLUMN` で勝手に補うと、旧データの意味（DEFAULT の決定など）を誤って解釈する危険があります。job_queue はジョブ履歴の安全性を優先します。

### 余分なカラムがある場合（forward-compat）

```
[migrations] jobs table has extra columns not known to this worker version: ['future_priority', 'future_tags']
```

これは warning だけで startup は続行します。新版 worker が追加したカラムを旧版 worker が見たケースを想定しています。

### Migration 関数自体が例外を投げた場合

`apply_migrations` は migration 関数が成功した後にのみ `set_user_version` を呼ぶので、例外時は `user_version` は **bump されません**。次回起動時に同じ migration を **再試行** します（一部適用済みの状態が残っている場合は手動修復が必要なケースもある — それは migration 関数側で対処すべき）。

## 開発者向け: 将来の migration の追加手順

新しいスキーマ変更が必要になったとき:

1. **`migrations.py` に migration 関数を追加 (column 存在チェック付き)**

   新規 DB は `db.py` の `CREATE TABLE` で既に新カラムを持っているため、`ALTER TABLE` をそのまま実行すると "duplicate column name" エラーになります。事前に `PRAGMA table_info` で確認してから追加してください:

   ```python
   def migrate_002_add_priority(conn: sqlite3.Connection) -> None:
       """Add ``priority`` column to ``jobs``, defaulting to 0."""
       cur = conn.execute("PRAGMA table_info(jobs)")
       existing = {row[1] for row in cur.fetchall()}
       if "priority" not in existing:
           conn.execute(
               "ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 0"
           )
   ```

2. **`MIGRATIONS` に追加**

   ```python
   MIGRATIONS = [
       (1, migrate_001_initial),
       (2, migrate_002_add_priority),  # ← 追加
   ]
   ```

3. **`db.py` の `CREATE TABLE IF NOT EXISTS` も新カラムを含むように更新**

   新規 DB は `CREATE TABLE` で `priority` を持って作られ、既存 DB は migration 002 で追加される、という二段構えになります。順序は変わらず:
   - `_init_schema_and_migrations` は `CREATE TABLE` → `apply_migrations` の順
   - 新規 DB: `CREATE TABLE` で全カラム、`migrate_001_initial` は v1 columns 検証 + stamp、`migrate_002_add_priority` は `priority` が既に存在するので no-op
   - 既存 v1 DB: `CREATE TABLE` no-op、`migrate_001_initial` は stamp 済みなので skip、`migrate_002_add_priority` が `ALTER TABLE` で `priority` を追加
   - 既存 v0 (lazy-v2.10.x) DB: `CREATE TABLE` no-op、`migrate_001_initial` が v1 columns 検証 → user_version=1、続けて `migrate_002_add_priority` が `ALTER TABLE` → user_version=2

4. **⚠️ `V1_JOBS_COLUMNS` は触らない**

   絶対に `V1_JOBS_COLUMNS` に新カラムを追加してはいけません。lazy-v2.10.x ユーザーが lazy-v2.12.0+ へ直接アップグレードした際に migration 001 が "missing column" で失敗し、migration 002 に到達できなくなります。

   各 migration は **自身が追加する列の責任** だけを持ち、過去の migration は変更しません。「最新スキーマ全体の検証」が必要なら migration 002 以降の中で別途行ってください。

5. **`tests/test_db_migrations.py` にテストを追加**
   - 新規 DB が user_version=2 で stamp される
   - 既存 v1 DB が migration 002 を経て user_version=2 になり、既存行の priority が default 0 になる (`ALTER TABLE ... DEFAULT` の挙動確認)
   - **直接アップグレードテスト (必須)**: lazy-v2.10.x 相当の v0 DB (V1_JOBS_COLUMNS のみ、priority なし) を作成し、JobStore を起動 → `user_version=2` になり、既存行が残り、`priority` 列が default 0 で追加されることを確認。これは migration 001 / 002 のチェーンが直列に動く保証です
   - rollback テストは不要（backward migration はサポートしない）

## 手動検証

### bash

```bash
# 既存 DB を退避してから新版で起動
mv .claude/queue/jobs.db .claude/queue/jobs.db.bak

# Python から JobStore を作って user_version を確認
python -c "
import sys
sys.path.insert(0, '.claude/skills/mcp-async-skill/scripts')
from job_queue.db import JobStore
store = JobStore('.claude/queue/jobs.db')
v = store.conn.execute('PRAGMA user_version').fetchone()[0]
print(f'user_version = {v}')
store.close()
"
# → user_version = 1
```

### PowerShell

```powershell
Move-Item .claude\queue\jobs.db .claude\queue\jobs.db.bak -Force -ErrorAction SilentlyContinue

python -c @"
import sys
sys.path.insert(0, '.claude/skills/mcp-async-skill/scripts')
from job_queue.db import JobStore
store = JobStore(r'.claude\queue\jobs.db')
v = store.conn.execute('PRAGMA user_version').fetchone()[0]
print(f'user_version = {v}')
store.close()
"@
# → user_version = 1
```

### sqlite3 CLI で直接確認

```bash
sqlite3 .claude/queue/jobs.db "PRAGMA user_version;"
# → 1
```

## 削除時期

`lazy-v2.13.0 以降で削除予定` の機能はありません。マイグレーションフレームワーク自体は今後永続的に使われます。

## 設計判断

- **Python 関数ベース** (SQL ファイルではない): エラーハンドリング、条件分岐、`PRAGMA` 確認を素直に書けるため。SQL ファイルはレビューしやすいが、複雑なケースを表現しにくい。
- **`Callable[[sqlite3.Connection], None]` の明示的型注釈**: 将来 migration を追加する開発者が signature を間違えないよう、型チェッカで検知できる形に。
- **forward-compat (extra columns はログだけ)**: 旧 worker が新 DB を読むケースを想定。新 worker が追加したカラムを旧 worker が知らなくても、最低限既存機能は動く。
- **backward migration は非サポート**: ダウングレードは「旧版 tarball を取り直して、DB は退避して新規作成」というユーザー操作を期待します。自動 rollback の複雑さに見合うユースケースが Phase 1 では想定されません。
- **migration 001 の precondition は db.py の docstring と相互参照**: 「順序が保証されている」契約を片側だけで暗黙に持つと、将来のリファクタで割れやすいため両側に書きます。
