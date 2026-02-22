# CLAUDE.md

## リポジトリ構成

- `origin`: Yumeno/LazyKamuiCodeSkillsCreator（開発用）
- `upstream`: el-el-san/KamuiCodeSkillsCreator（フォーク元、参照専用）

## 開発ルール

### Issue / PR の作成先

- Issue・PR はすべて `Yumeno/LazyKamuiCodeSkillsCreator` に作成すること
- `upstream` (el-el-san) には Issue・PR を作成しない
- `gh pr create` 実行時は `--repo Yumeno/LazyKamuiCodeSkillsCreator` を明示する
- `upstream` remote は参照用として残すが push には使用しない

### テスト

- テスト実行: `python -m pytest .claude/skills/mcp-async-skill/scripts/tests/ -q`
- コミット前にテストが全件パスすることを確認する
