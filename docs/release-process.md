# リリース作業手順

新バージョンをリリースする手順です。

## 前提

- GitHub Actions ワークフロー（`.github/workflows/release.yml`）が設定済み
- リリースパッケージのスケルトン（`release/claude/`）がコミット済み
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

1. **スクリプト同期** — `.claude/skills/mcp-async-skill/` から `release/claude/lazykamui/.claude/` へコピー
   - `scripts/generate_skill.py`
   - `scripts/mcp_async_call.py`
   - `scripts/mcp_worker_daemon.py`
   - `scripts/job_queue/` パッケージ一式
   - `SKILL.md`
   - テスト・`__pycache__` は除外
2. **バージョン書き換え** — `pyproject.toml` と `__init__.py` の version をタグから反映
3. **コミット＆push** — `release: update release/claude/ to lazy-v2.0.0` として main に push
4. **タグ更新** — リリースファイルを含む状態でタグを force-update

### 4. 動作確認

```bash
# pip install で確認（バージョン指定）
pip install git+https://github.com/Yumeno/LazyKamuiCodeSkillsCreator.git@lazy-v2.0.0#subdirectory=release/claude

# CLI が動くか
generate-skill --help

# バージョン確認
python -c "import lazykamui; print(lazykamui.__version__)"
```

### 5. （任意）GitHub Releases にノートを追加

```bash
gh release create lazy-v2.0.0 --title "lazy-v2.0.0" --notes "リリースノート内容"
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
  ├─ タグからバージョン抽出（lazy-v2.0.0 → 2.0.0）
  ├─ .claude/skills/ → release/claude/lazykamui/.claude/ にスクリプトをコピー
  ├─ pyproject.toml / __init__.py のバージョンを書き換え
  ├─ main にコミット＆push
  └─ タグを force-update（リリースファイル込みの状態に更新）
```

タグの force-update により、`pip install ...@lazy-v2.0.0` で正しいバージョンのスクリプトが取得されます。

## トラブルシューティング

### CI が失敗した場合

1. GitHub Actions のログを確認
2. 修正を main に push
3. タグを削除して再作成：
   ```bash
   git tag -d lazy-v2.0.0
   git push origin :refs/tags/lazy-v2.0.0
   git tag lazy-v2.0.0
   git push origin lazy-v2.0.0
   ```

### ローカルでリリースパッケージを確認したい場合

```bash
# スクリプトを手動で同期
SRC=.claude/skills/mcp-async-skill
DST=release/claude/lazykamui/.claude/skills/mcp-async-skill
rm -rf $DST/scripts $DST/SKILL.md
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

# editable install でテスト
cd release/claude
pip install -e .
generate-skill --help
```

> **注意**: `release/claude/lazykamui/.claude/` は `.gitignore` に含まれているため、ローカルで同期したファイルはコミットされません。CI のみがこれらのファイルをコミットします。
