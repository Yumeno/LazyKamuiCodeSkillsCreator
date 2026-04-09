# リリース作業手順

新バージョンをリリースする手順です。

## 前提

- GitHub Actions ワークフロー（`.github/workflows/release.yml`）が設定済み
- main ブランチが最新の状態

## 手順

### 1. リリース内容の確認

リリースに含めたい変更がすべて main にマージされていることを確認します。

```bash
git checkout main
git pull
```

### 2. バージョンタグを作成・push

`lazy-v` プレフィックス付きのセマンティックバージョニングタグを push します。

```bash
git tag lazy-v2.0.0
git push origin lazy-v2.0.0
```

### 3. GitHub Actions の自動処理

タグ push をトリガーに、以下が自動実行されます：

1. **tar.gz ビルド** — `.claude/skills/mcp-async-skill/` から必要ファイルを収集し `mcp-async-skill.tar.gz` を作成
   - `scripts/generate_skill.py`
   - `scripts/mcp_async_call.py`
   - `scripts/mcp_worker_daemon.py`
   - `scripts/job_queue/` パッケージ一式
   - `SKILL.md`
   - テスト・`__pycache__` は除外
2. **GitHub Release 作成** — タグ名でリリースを作成し、tar.gz をアセットとしてアップロード
3. **リリースノート自動生成** — インストールコマンド（bash / PowerShell）を含むノートが自動記載

### 4. 動作確認

```bash
# ダウンロード・展開テスト
mkdir -p /tmp/test-project/.claude/skills
curl -fSL -o /tmp/mcp-async-skill.tar.gz \
  https://github.com/Yumeno/LazyKamuiCodeSkillsCreator/releases/download/lazy-v2.6.3/mcp-async-skill.tar.gz
tar xzf /tmp/mcp-async-skill.tar.gz -C /tmp/test-project/.claude/skills/

# 展開結果の確認
ls /tmp/test-project/.claude/skills/mcp-async-skill/
# → SKILL.md  scripts/

# generate_skill.py の動作確認
python3 /tmp/test-project/.claude/skills/mcp-async-skill/scripts/generate_skill.py --help
```

## バージョニング規則

[セマンティックバージョニング](https://semver.org/lang/ja/) に従います：

| 変更内容 | バージョン | 例 |
|----------|-----------|-----|
| 後方互換性のないAPI変更 | メジャー | lazy-v2.0.0 → lazy-v3.0.0 |
| 後方互換性のある機能追加 | マイナー | lazy-v2.0.0 → lazy-v2.1.0 |
| バグ修正 | パッチ | lazy-v2.0.0 → lazy-v2.0.1 |

## CI ワークフローの仕組み

```
lazy-v* タグ push
  │
  ├─ checkout
  ├─ .claude/skills/mcp-async-skill/ からスクリプトを収集
  ├─ mcp-async-skill.tar.gz を作成（テスト等は除外）
  └─ GitHub Release を作成し tar.gz をアセットとしてアップロード
```

ユーザーは GitHub Releases から tar.gz をダウンロードし、プロジェクトの `.claude/skills/` に展開します。

## トラブルシューティング

### CI が失敗した場合

1. GitHub Actions のログを確認
2. 修正を main に push
3. リリースとタグを削除して再作成：
   ```bash
   gh release delete lazy-v2.0.0 --yes
   git tag -d lazy-v2.0.0
   git push origin :refs/tags/lazy-v2.0.0
   git tag lazy-v2.0.0
   git push origin lazy-v2.0.0
   ```

### ローカルで tar.gz をビルドして確認したい場合

```bash
SRC=.claude/skills/mcp-async-skill
STAGING=$(mktemp -d)
DST=$STAGING/mcp-async-skill

mkdir -p $DST/scripts/job_queue
cp $SRC/scripts/generate_skill.py $DST/scripts/
cp $SRC/scripts/mcp_async_call.py $DST/scripts/
cp $SRC/scripts/mcp_worker_daemon.py $DST/scripts/
cp $SRC/scripts/job_queue/__init__.py $DST/scripts/job_queue/
cp $SRC/scripts/job_queue/db.py $DST/scripts/job_queue/
cp $SRC/scripts/job_queue/dispatcher.py $DST/scripts/job_queue/
cp $SRC/scripts/job_queue/worker.py $DST/scripts/job_queue/
cp $SRC/scripts/job_queue/client.py $DST/scripts/job_queue/
cp $SRC/SKILL.md $DST/

tar czf mcp-async-skill.tar.gz -C $STAGING mcp-async-skill
echo "Built: mcp-async-skill.tar.gz ($(du -h mcp-async-skill.tar.gz | cut -f1))"

# 展開テスト
TEST_DIR=$(mktemp -d)
mkdir -p $TEST_DIR/.claude/skills
tar xzf mcp-async-skill.tar.gz -C $TEST_DIR/.claude/skills/
ls $TEST_DIR/.claude/skills/mcp-async-skill/
```
