# WSL2 + tmux + Claude Code サブエージェント並列開発 セットアップ手順

Issue #21〜#25 をサブエージェントで並列開発するための環境構築手順。

## 前提

- Windows 11 Pro
- Windows Terminal（PowerShell から WSL2 に入る）
- WSL2 + Ubuntu 24.04 (インストール済み)
- GitHub リポジトリ: `Yumeno/LazyKamuiCodeSkillsCreator`

> **検証済み**: Windows Terminal (PowerShell) → WSL2 → tmux のペイン分割は正常に動作する。
> xterm 等の GUI ターミナルは不要。日本語表示・入力も Windows Terminal が処理するため追加設定不要。

---

## なぜ Agent Teams ではなくサブエージェントか

| | Agent Teams | サブエージェント (Task) |
|---|---|---|
| ワークツリー自動分離 | **しない**（共有ディレクトリ） | **する**（`isolation: worktree`） |
| 承認 | リーダーに集約。都度承認が必要 | バックグラウンドなら**事前一括承認** |
| 並列ブランチ作業 | 同じディレクトリで衝突する | ワークツリーで安全に分離 |
| 可視化 | tmux ペイン自動分割 | トランスクリプト tail で監視 |

今回は issue ごとに別ブランチで並列修正するため、**サブエージェント + worktree 分離**が適切。

Agent Teams は「同じブランチで、ファイルが被らない協調作業」向き。

---

## 1. WSL2 Ubuntu に必要なツールを入れる

Windows Terminal の PowerShell タブから:

```powershell
wsl -d Ubuntu-24.04
```

WSL2 内で:

```bash
# パッケージ更新
sudo apt update && sudo apt upgrade -y

# tmux インストール（モニタリング用）
sudo apt install -y tmux jq

# Claude Code CLI インストール
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
npm install -g @anthropic-ai/claude-code

# gh CLI インストール（PR/issue 操作用）
sudo apt install -y gh

# gh で GitHub 認証
gh auth login
```

## 2. リポジトリを WSL2 ネイティブにクローン

`/mnt/c/` 経由は遅いので、WSL2 内のファイルシステムに置く。

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/Yumeno/LazyKamuiCodeSkillsCreator.git
cd LazyKamuiCodeSkillsCreator
```

> **注意: upstream は設定しない**
>
> `git remote add upstream https://github.com/el-el-san/KamuiCodeSkillsCreator.git` は
> ここでは追加しないこと。`gh` コマンドが upstream を優先して issue や PR を
> fork 元（el-el-san）に登録してしまう場合がある。
> upstream との同期が必要な場合は、明示的に remote を指定して操作する:
> ```bash
> git fetch https://github.com/el-el-san/KamuiCodeSkillsCreator.git main
> ```

## 3. ツール実行の事前承認を設定

サブエージェントがバックグラウンドで自律的に動くために、よく使う操作を事前承認する。

```bash
mkdir -p .claude

cat > .claude/settings.json << 'EOF'
{
  "permissions": {
    "allow": [
      "Bash(git *)",
      "Bash(gh issue *)",
      "Bash(gh pr *)",
      "Bash(python *)",
      "Bash(pytest *)",
      "Bash(pip *)",
      "Edit",
      "Write",
      "Read",
      "Glob",
      "Grep"
    ]
  }
}
EOF
```

> **解説**: サブエージェントはバックグラウンド起動時に事前承認を求める。
> ここで許可しておけば、実行中に都度承認する必要がなくなる。
> `--dangerously-skip-permissions` を使う手もあるが、上記の方が安全。

## 4. サブエージェント用のカスタムエージェント定義（任意）

ワークツリー分離を明示的に設定したカスタムエージェントを作成できる:

```bash
mkdir -p .claude/agents

cat > .claude/agents/issue-fixer.md << 'EOF'
---
name: issue-fixer
description: GitHub issue の修正を担当するサブエージェント
isolation: worktree
tools: Read, Write, Edit, Glob, Grep, Bash
---

あなたは GitHub issue の修正を担当するエージェントです。

## 作業手順

1. 指定された issue の内容を `gh issue view` で確認する
2. 関連するソースコードを読み、原因を特定する
3. 修正ブランチを作成する（`fix/<issue-slug>`）
4. コードを修正する
5. テストがあれば実行する
6. 変更をコミットする
7. リモートにプッシュし、PR を作成する（`gh pr create`）

## 注意

- PR/issue は必ず origin (Yumeno/LazyKamuiCodeSkillsCreator) に対して作成する
- upstream (el-el-san) には絶対に issue/PR を作成しない
EOF
```

> **`isolation: worktree`** がポイント。
> これにより各サブエージェントが自動的に一時的なワークツリーで動作し、
> ブランチの衝突が起きない。ワークツリーはサブエージェント完了後に自動クリーンアップされる。

## 5. Windows Terminal から tmux + Claude Code を起動

Windows Terminal の PowerShell タブで:

```powershell
wsl -d Ubuntu-24.04
```

WSL2 内で:

```bash
cd ~/projects/LazyKamuiCodeSkillsCreator

# tmux セッション作成（ウィンドウを最大化してから実行推奨）
tmux new-session -s dev

# tmux 内で Claude Code 起動
claude
```

## 6. Issue を並列開発する（指示例）

Claude Code のプロンプトの書き方例。

> **重要**: 「ブランチを分けて作業して」だけだと、LLM は1つずつ順番に処理してしまう。
> **Task ツール**、**同時に**、**バックグラウンド** を明示的に指定すること。

### カスタムエージェント (issue-fixer) を使う場合

```
Task ツールを使って、以下の3つの issue を3つのサブエージェント (issue-fixer) で
**同時に** バックグラウンドで処理してください。
1つずつ順番にではなく、3つの Task を1回のメッセージで同時に起動してください。

各サブエージェントは isolation: worktree で自動的にワークツリー分離されます。

- Task 1: #<issue番号> <概要>
- Task 2: #<issue番号> <概要>
- Task 3: #<issue番号> <概要>

3つとも完了したら、結果をまとめて報告してください。
```

### カスタムエージェントなしで直接指示する場合

```
Task ツールの subagent_type: "general-purpose" で、以下の3つの issue を
**同時に3つのバックグラウンド Task として** 起動してください。
1つずつ順番にではなく、1回のレスポンスで3つの Task tool call を並列発行してください。

各 Task には以下を指示してください:
- git worktree を作成して分離された環境で作業する
- gh issue view で issue 内容を確認する
- fix/<slug> ブランチを作成する
- コードを修正し、テストがあれば実行する
- コミット → プッシュ → gh pr create で PR 作成
- PR/issue は必ず origin (<owner>/<repo>) に対して作成する

Issue:
- Task 1: #<issue番号> <概要>
- Task 2: #<issue番号> <概要>
- Task 3: #<issue番号> <概要>
```

> **依存関係がある issue の扱い**: 同じファイルを変更する issue 同士は並列にせず、
> 先行 issue の完了後に順番に対応するよう指示に含める。

## 7. サブエージェントの進捗をモニタリング

tmux でペインを分割して、サブエージェントの活動を監視する:

```bash
# Ctrl+B → % で縦分割
# 新しいペインで:

# トランスクリプトの更新を監視
watch -n 2 'ls -lt ~/.claude/projects/*/subagents/agent-*.jsonl 2>/dev/null | head -5'

# 特定のサブエージェントの詳細を見る場合:
# tail -f ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl | jq '.type'
```

Claude Code 内では:
- `Ctrl+T` でタスク一覧を表示
- 完了したサブエージェントの結果はメインセッションに報告される

---

## tmux 基本操作

| 操作 | キー |
|------|------|
| ペイン分割（縦） | `Ctrl+B` → `%` |
| ペイン分割（横） | `Ctrl+B` → `"` |
| ペイン移動 | `Ctrl+B` → 矢印キー |
| セッション一覧 | `Ctrl+B` → `s` |
| デタッチ（裏に回す） | `Ctrl+B` → `d` |
| リアタッチ | `tmux attach -t dev` |
| スクロールモード | `Ctrl+B` → `[` → 矢印/PgUp で移動 → `q` で抜ける |

---

## トラブルシューティング

### Claude Code が tmux を検出しない

```bash
# tmux 内で確認
echo $TMUX
# 空なら tmux 内ではない → tmux attach -t dev
```

### サブエージェントの承認が求められる

`.claude/settings.json` の `permissions.allow` に必要なツールが含まれているか確認。
サブエージェント起動前に事前承認プロンプトが出る場合は承認する。

### tmux の表示がおかしい

Windows Terminal の設定でフォントを等幅フォント（Cascadia Code 等）にする。
また、Windows Terminal 自体のペイン分割（`Alt+Shift++` 等）と tmux のペインを混在させないよう注意。

### `/mnt/c/` のファイルが遅い

WSL2 ネイティブの `~/projects/` を使う。Windows 側から WSL2 のファイルにアクセスするには
エクスプローラーで以下を開く:
```
\\wsl$\Ubuntu-24.04\home\<username>\projects\
```

### 日本語が文字化けする

Windows Terminal が日本語を処理するため通常は問題ない。
万一 WSL2 側で文字化けする場合:
```bash
sudo locale-gen ja_JP.UTF-8
echo 'export LANG=ja_JP.UTF-8' >> ~/.bashrc
source ~/.bashrc
```
