# Custom Groups (Endpoint-Pattern Rate Limiting)

`lazy-v2.11.0` 以降、`category_rate_limits` (`t2i` / `i2i` / `t2v` / `i2v` カテゴリ単位の制御) に加えて、**ユーザーが endpoint パターンで定義する独自グループ** に対しても per-group の同時実行数 / インターバル / 429 cooldown を設定できます。

## なぜ必要か

カテゴリ別レートリミット ([per-category limits](category-limits.md)) は「サービスの利用形態 (テキスト→画像 / 画像→画像 / テキスト→動画 / 画像→動画)」という粗い粒度で十分機能します。一方で、上流の MCP サービスは **特定の高コストモデル群だけを別枠で絞る** 運用をしているケースがあります：

- Bytedance Seedance v2.0 の動画モデル群は `t2v` / `i2v` の通常モデルとは独立したクオータで管理されている
- 同じ `t2v` カテゴリでも、Veo3 のような high-tier モデルは別枠
- ユーザー独自の事情で「特定モデルだけ絞りたい」「特定モデルだけ並列度を上げたい」

これらをカテゴリ単位で表現すると、以下のいずれかの問題が起きます：

1. **カテゴリ全体を厳しく絞る** → 安価なモデルまで巻き込まれて待たされる
2. **カテゴリ全体を緩める** → 高コストモデルが上流レートリミットを踏んで 429 連発

`custom_groups` はこの間を埋める仕組みです。glob パターンで対象 endpoint を選び、その群だけ独立した limiter を持たせます。**マッチした群はカテゴリ計上から完全に除外** されるので、ダブルカウントによる過剰絞りも起きません。

## 設定スキーマ (`queue_config.json`)

```json
{
  "custom_groups": {
    "t2v_sd2": {
      "endpoints": [
        "https://kamui-code.ai/t2v_sd2/fal/bytedance/seedance-v2.0",
        "https://kamui-code.ai/t2v_sd2/fal/bytedance/seedance-v2.0-fast"
      ],
      "max_inflight": 1,
      "min_interval": 10,
      "exhaust_cooldown": 3600
    },
    "premium-video": {
      "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
      "max_inflight": 1,
      "min_interval": 30,
      "exhaust_cooldown": 7200
    }
  }
}
```

各キー：

| キー | 意味 | 既定値 (key 未指定時) |
|---|---|---|
| `endpoints` | マッチさせる endpoint URL の glob パターン (fnmatch ベース、case-sensitive)。配列で複数指定可 | **必須**（空配列 / 未指定はそのグループを無視） |
| `max_inflight` | 同時に submit 中にできるジョブ数 | `1` |
| `min_interval` | 同グループ内の submit 間隔 (秒) | `1.0` |
| `exhaust_cooldown` | 429 を 1 回受けた後にグループ全体を抑制する秒数 | `3600` |

### Glob パターン

- `fnmatch.fnmatchcase` ベース。**大文字小文字を区別** します（URL は実運用上 case-sensitive のため）
- ワイルドカード: `*` (任意の文字列)、`?` (任意の 1 文字)、`[abc]` (文字クラス)
- パスの `/` を特別扱いしません — 普通の文字としてマッチングされます

例：

| パターン | マッチする例 | しない例 |
|---|---|---|
| `https://kamui-code.ai/t2v/fal/veo3*` | `.../veo3`, `.../veo3-fast`, `.../veo3-pro` | `.../veo` |
| `*/seedance*` | 任意のホスト下の `seedance*` | `seedanceX` の前に `/` がない URL |
| `https://api.example.com/*` | 当該ホスト配下の任意 path | 別ホスト |

### マッチング順序: First-Match-Wins

複数の群が同じ endpoint にマッチする可能性がある場合、**`queue_config.json` での宣言順で最初にマッチした群が採用** されます。Python 3.7+ で `dict` の挿入順が保証されるため、JSON での記述順 = Python での走査順です。

```json
{
  "custom_groups": {
    "specific": {
      "endpoints": ["https://kamui-code.ai/t2v/fal/veo3-pro"],
      "max_inflight": 1
    },
    "broad": {
      "endpoints": ["https://kamui-code.ai/t2v/*"],
      "max_inflight": 5
    }
  }
}
```

→ `https://kamui-code.ai/t2v/fal/veo3-pro` は **`specific` 群** に割り当てられます (`broad` も match するが、宣言順で `specific` が先)。

### Group 名の制約

- HTTP API (`POST /api/groups/{name}/{action}`) の URL path に直接埋め込むため、**simple token (英数 + `-` + `_`) を推奨** します
- `/`, 空白, `?`, `#` 等は API path として扱いにくいので避けてください
- 内部的にはどんな文字でも config 上は使えますが、HTTP API 経由の操作ができなくなります

## カテゴリとの関係: マッチした群はカテゴリから完全除外

設計上の重要ポイントです。

```
endpoint = https://kamui-code.ai/t2v_sd2/fal/bytedance/seedance-v2.0

  Step 1: custom_groups にマッチするか?
    YES → 群の limiter を使う、カテゴリ計上には含めない
    NO  ↓
  Step 2: extract_category() で category prefix を取得
    マッチ → category limiter を使う
    None  → 何の rate limit も適用しない (dispatcher は throttle なしで dispatch)
```

これにより、`t2v_sd2` 群にマッチした endpoint の 429 / inflight は **`t2v` カテゴリのカウンタには加算されません**。`t2v_sd2` を厳しく絞っても、別の通常 `t2v` モデル (例: `wan-25-preview`) の dispatch には影響しません。

## 同梱されているデフォルト群

`lazy-v2.11.0` 以降の新規スキル生成 (`generate_skill.py`) と `queue_config.example.json` には、Bytedance Seedance v2.0 動画モデル群が **デフォルトで同梱** されます：

| 群名 | 対象 endpoint | max_inflight | min_interval | exhaust_cooldown |
|---|---|---|---|---|
| `t2v_sd2` | `seedance-v2.0` / `seedance-v2.0-fast` | 1 | 10s | 3600s |
| `i2v_sd2` | `seedance-v2.0` / `seedance-v2.0-fast` | 1 | 10s | 3600s |
| `r2v_sd2` | `seedance-v2.0-reference` / `seedance-v2.0-fast-reference` | 1 | 10s | 3600s |

なぜ初期同梱かというと、**URL prefix `t2v_sd2` / `i2v_sd2` / `r2v_sd2` が標準カテゴリリスト (`t2i / i2i / t2v / i2v`) に含まれない** ため、`custom_groups` 設定なしでは「未知 endpoint」扱いとなり、dispatcher が rate-limit accounting を一切スキップしてしまうからです。高コストモデルがレートリミットなしで野放しになるのは安全とは言えないため、保守的なデフォルトを最初から入れています。

これらの値は実運用での挙動を見て自由に調整してください。

## Runtime API

実行中の worker に対して `PATCH /api/config` で per-group 値を変更できます。

### 群の状態取得 (`GET /api/groups`)

#### bash

```bash
curl http://127.0.0.1:54321/api/groups
```

#### PowerShell

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:54321/api/groups" | ConvertTo-Json -Depth 5
```

レスポンス例：

```json
{
  "server_time_utc": "2026-05-10T07:30:00.000000Z",
  "groups": {
    "t2v_sd2": {
      "endpoints": [
        "https://kamui-code.ai/t2v_sd2/fal/bytedance/seedance-v2.0",
        "https://kamui-code.ai/t2v_sd2/fal/bytedance/seedance-v2.0-fast"
      ],
      "paused": false,
      "inflight": 0,
      "max_inflight": 1,
      "min_interval": 10,
      "exhaust_cooldown": 3600,
      "consecutive_429": 0,
      "cooldown_remaining_s": 0
    },
    "i2v_sd2": { ... },
    "r2v_sd2": { ... }
  }
}
```

### Per-group 値の runtime 変更 (`PATCH /api/config`)

#### bash

```bash
# 1 つの群の max_inflight を変更
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"groups": {"t2v_sd2": {"max_inflight": 2}}}'

# 複数群を同時に更新
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"groups": {
    "t2v_sd2": {"max_inflight": 2, "exhaust_cooldown": 1800},
    "i2v_sd2": {"max_inflight": 2}
  }}'
```

#### PowerShell

```powershell
Invoke-RestMethod -Method Patch `
  -Uri "http://127.0.0.1:54321/api/config" `
  -Body (@{
      groups = @{
          t2v_sd2 = @{ max_inflight = 2; exhaust_cooldown = 1800 }
          i2v_sd2 = @{ max_inflight = 2 }
      }
  } | ConvertTo-Json -Depth 5) `
  -ContentType "application/json"
```

レスポンス例：

```json
{
  "applied": {
    "groups.t2v_sd2.max_inflight": 2,
    "groups.t2v_sd2.exhaust_cooldown": 1800,
    "groups.i2v_sd2.max_inflight": 2
  },
  "rejected": {},
  "requires_restart": []
}
```

### 群の手動 pause / resume

サービス障害時や緊急停止用：

#### bash

```bash
# pause
curl -X POST http://127.0.0.1:54321/api/groups/t2v_sd2/pause

# resume
curl -X POST http://127.0.0.1:54321/api/groups/t2v_sd2/resume
```

#### PowerShell

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:54321/api/groups/t2v_sd2/pause"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:54321/api/groups/t2v_sd2/resume"
```

未知の群名を指定した場合は **404** が返り、レスポンス本文に `available_groups` リストが含まれます：

```json
{
  "error": "Unknown group: nonexistent",
  "available_groups": ["t2v_sd2", "i2v_sd2", "r2v_sd2"]
}
```

### 入力検証

`PATCH /api/config` の `groups` 値は dict 必須です。`null` / `int` / `list` / `string` 等は reject されます (per-category と同じパターン)：

```bash
# 拒否される例
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"groups": [1, 2, 3]}'
# → rejected: {"groups": "must be object (got list)"}

# 未知の群名も reject
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"groups": {"nonexistent": {"max_inflight": 1}}}'
# → rejected: {"groups.nonexistent": "unknown group"}
```

## 後方互換マトリクス

| 利用者の状態 | queue_config | dispatcher | 動作 |
|---|---|---|---|
| **新規インストール** | デフォルトで Seedance v2.0 群が入っている | 新 (v2.11.0+) | 高コストモデルが最初から rate-limited |
| **lazy-v2.10.x からアップグレード、`queue_config.json` を手動で残した** | `custom_groups` キーが無い | 新 (v2.11.0+) | `custom_groups` は空 dict 扱い、全 endpoint がカテゴリ経由 (= 従来通り) |
| **lazy-v2.10.x dashboard を使い続ける** | (任意) | 新 (v2.11.0+) | dashboard は `custom_groups` を表示しないが、worker 側は問題なく動作 |

`custom_groups` の追加は **完全に opt-in** です。既存の `queue_config.json` に `custom_groups` キーを追加しなくても、PR1 で導入された per-category limits だけで動作し続けます。

## 設計判断ログ

PR4 (#60) で議論し決定した仕様：

- **群が category を完全 bypass する**: マッチした endpoint は群の limiter だけで accounting される。カテゴリにも同時加算するとダブルカウントになり、ユーザーの直感 (「群を独立に管理したい」) に反する
- **First-match-wins (宣言順)**: 同じ endpoint が複数群にマッチする可能性がある場合、宣言順で勝ち。Python 3.7+ の dict 挿入順保証に依存。優先度を変えたいときは config ファイル内で群の宣言順を入れ替える
- **未知の群名は 404**: HTTP API として典型的な扱い。`available_groups` リストを同時に返してデバッグを楽にする
- **`PATCH /api/config` の `groups` 値は dict 必須**: per-category と同じ contract。`null` / `int` 等を silent に通すと設定ミスを見逃すため
- **状態管理は `LimiterStateMixin` で `CategoryLimiter` と共有**: inflight / 429 cooldown / pause/resume の挙動が両者で完全に同じであることを保証。バグ修正・拡張が一箇所で済む
- **`_match_cache` の lock 内化 (PHASE1_PLAN_v3 fix #13)**: glob マッチング結果をキャッシュするが、複数 dispatcher thread からの同時 read/write を保証するため `_lock` 内で操作。`_SENTINEL` で「未計算」と「キャッシュ済み None」を区別
