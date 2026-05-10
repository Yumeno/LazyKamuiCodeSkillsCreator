# Per-Category Rate Limits

`lazy-v2.11.0` 以降、カテゴリ (`t2i` / `i2i` / `t2v` / `i2v` / `r2v`) ごとに inflight・min_interval・429 cooldown を **独立した値で設定可能**になりました。アップストリーム MCP サービス側もカテゴリごとにローリング窓レートリミットを分けて運用しているため、クライアント側でも対応する個別値を設定できます。

> **`r2v` の独立化** (`lazy-v2.11.1+`): 当初 `r2v` は `i2v` の alias として実装されていましたが、アップストリーム MCP サービスが `r2v` を `i2v` と独立にレートリミットしていることが確認されたため、`r2v` を独立カテゴリとして扱うようになりました。旧 `queue_config.json` に `aliases.r2v` 設定が残っていても、起動時に自動で無視され警告ログが 1 回出力されます (詳細は本ページ末尾の「`r2v` の取り扱い」を参照)。

## 設定スキーマ (`queue_config.json`)

### 新形式 (`lazy-v2.11.0+` 推奨)

```json
{
  "category_rate_limits": {
    "categories": ["t2i", "i2i", "t2v", "i2v", "r2v"],
    "aliases": {"r2i": "i2i"},
    "limits": {
      "t2i": {"max_inflight": 3, "min_interval": 1.0, "exhaust_cooldown": 600},
      "i2i": {"max_inflight": 2, "min_interval": 1.0, "exhaust_cooldown": 3600},
      "t2v": {"max_inflight": 1, "min_interval": 1.0, "exhaust_cooldown": 1800},
      "i2v": {"max_inflight": 1, "min_interval": 1.0, "exhaust_cooldown": 3600},
      "r2v": {"max_inflight": 1, "min_interval": 1.0, "exhaust_cooldown": 3600}
    }
  }
}
```

各キー：

| キー | 意味 | 既定値 (key 未指定時) |
|---|---|---|
| `max_inflight` | 同時に submit 中にできるジョブ数 | `1` |
| `min_interval` | 同カテゴリ内の submit 間隔 (秒) | `1.0` |
| `exhaust_cooldown` | 429 を 1 回受けた後にカテゴリ全体を抑制する秒数 | `3600` |

### 旧形式 (lazy-v2.10.x、後方互換ロード)

```json
{
  "category_rate_limits": {
    "categories": ["t2i", "i2i", "t2v", "i2v"],
    "aliases": {"r2i": "i2i", "r2v": "i2v"},
    "max_category_inflight": 1,
    "min_interval": 1.0,
    "exhaust_cooldown": 3600
  }
}
```

旧形式の値は **すべての設定済みカテゴリに同じ値で展開** されます。worker 起動時に `[CategoryLimiter] (instance ...) DEPRECATED: ...` という警告ログが 1 回出力されます。
旧形式は `lazy-v2.13.0 以降で削除予定` です。新規インストールでは新形式を使用してください。

> 旧形式の例で `aliases.r2v` が含まれている場合、`lazy-v2.11.1+` ではこの alias 設定は **強制的に削除** され、`r2v` URL は r2v カテゴリとして独立に集計されます (詳細は本ページ末尾の「`r2v` の取り扱い」を参照)。

### Fallback 挙動

設定漏れに対する保険として、`limits.{cat}` または個別キーが欠けている場合はモジュールレベルのハードコード初期値 (`max_inflight=1`, `min_interval=1.0`, `exhaust_cooldown=3600`) が使われます。**正規の運用では `limits` を 5 カテゴリ × 3 キー全て明示してください。**

### `r2v` の取り扱い (lazy-v2.11.1+)

過去の `queue_config.json` には `aliases: {"r2i": "i2i", "r2v": "i2v"}` のように、`r2v` (reference-to-video) を `i2v` の alias として宣言する設定が含まれていることがあります。

- `lazy-v2.11.1` 以降、`CategoryLimiter` は **コードレベルで `aliases.r2v` を強制的に無視** します。
- 起動時に `[CategoryLimiter] (instance ...) ignored forbidden alias keys in config (\`aliases.r2v\`): ...` という警告ログが 1 回出力されます。
- これにより、`r2v` URL は **常に独立した `r2v` カテゴリ** として集計され、`i2v` 枠を消費しません。
- `queue_config.json` 側の `aliases.r2v` を **削除し、`limits.r2v` を追加すること**を強く推奨します。
- なお `r2i` (reference-to-image) は引き続き `i2i` の alias として扱われます (アップストリームでは `r2i` の独立レートリミットの有無が未確認のため)。

## Runtime API (`PATCH /api/config`)

実行中の worker に対して、`PATCH /api/config` で per-category 値を変更できます。

### 新形式 (推奨)

特定カテゴリだけ変えたいとき：

#### bash

```bash
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"category": {"limits": {"t2v": {"max_inflight": 1, "exhaust_cooldown": 1800}}}}'
```

#### PowerShell

```powershell
Invoke-RestMethod -Method Patch `
  -Uri "http://127.0.0.1:54321/api/config" `
  -Body (@{
      category = @{
          limits = @{
              t2v = @{ max_inflight = 1; exhaust_cooldown = 1800 }
          }
      }
  } | ConvertTo-Json -Depth 5) `
  -ContentType "application/json"
```

レスポンス例：

```json
{
  "applied": {
    "category.limits.t2v.max_inflight": 1,
    "category.limits.t2v.exhaust_cooldown": 1800
  },
  "rejected": {},
  "requires_restart": []
}
```

### 旧形式 (lazy-v2.10.x dashboard 互換)

`lazy-v2.10.x dashboard` は新形式の per-category 入力 UI を持たず、旧形式 `{"category": {"max_inflight": N}}` を送ってきます。新 worker はこれを **全カテゴリへの一括適用** として受理します。

#### bash

```bash
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"category": {"max_inflight": 5}}'
```

レスポンス例：

```json
{
  "applied": {
    "_legacy_warning": "Received legacy flat category.{key} form; applied to all categories not covered by category.limits in this request. Migrate to category.limits.{cat}.{key} per-category form.",
    "category.max_inflight": {"value": 5, "affected": ["i2i", "i2v", "t2i", "t2v"]}
  },
  "rejected": {}
}
```

> **Warning**: 旧 `lazy-v2.10.x dashboard` から値編集 PATCH を投げると、新 worker は **全カテゴリに一括適用** します。最初のカテゴリの値だけを変えたつもりが全カテゴリに展開されるので注意してください。**per-category の独立編集を行うには `lazy-v2.11.0` 以降の dashboard (本リリース同梱) が必要** です。

### 入力検証

`category.limits` は dict 必須。`null` / `int` / `list` / `string` などを渡すと reject されます。これは「キーを書き忘れた」と「明示的に空にしたい」を区別するための仕様で、設定ミスを silent に通さない安全弁です。

```bash
# 拒否される例
curl -X PATCH http://127.0.0.1:54321/api/config \
  -H 'Content-Type: application/json' \
  -d '{"category": {"limits": null}}'
# → rejected: {"category.limits": "must be object (got NoneType)"}
```

## 状態の確認 (`GET /api/config`, `GET /api/categories`)

#### bash

```bash
# 現在の per-category 設定値を取得
curl http://127.0.0.1:54321/api/config

# カテゴリ別のリアルタイム状態 (inflight, cooldown_remaining_s, paused, etc.)
curl http://127.0.0.1:54321/api/categories
```

#### PowerShell

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:54321/api/config" |
  Select-Object -ExpandProperty category | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:54321/api/categories"
```

`GET /api/config` のレスポンス例：

```json
{
  "category": {
    "limits": {
      "t2i": {"max_inflight": 3, "min_interval": 1.0, "exhaust_cooldown": 600},
      "i2i": {"max_inflight": 2, "min_interval": 1.0, "exhaust_cooldown": 3600},
      "t2v": {"max_inflight": 1, "min_interval": 1.0, "exhaust_cooldown": 1800},
      "i2v": {"max_inflight": 1, "min_interval": 1.0, "exhaust_cooldown": 3600}
    },
    "max_inflight": 2,
    "min_interval": 1.0,
    "exhaust_cooldown": 3600
  },
  "endpoint": {...},
  "worker": {...}
}
```

`category.max_inflight` / `category.min_interval` / `category.exhaust_cooldown` は **lazy-v2.10.x dashboard 互換のためのミラーキー**で、`limits` を辞書順に並べたときの最初のカテゴリ (i2i) の値が入ります。`lazy-v2.13.0 以降で削除予定` です。

## 後方互換マトリクス

PR1 (lazy-v2.11.0) は次の組み合わせをすべてサポートします：

| 利用者の状態 | queue_config | dispatcher | dashboard | 動作 |
|---|---|---|---|---|
| **新規インストール** | 新形式 (limits) | 新 (v2.11.0+) | 新 (v2.12.0+, 後続 PR) | フル機能 |
| **mcp-async-skill のみ更新** | 旧形式 (legacy) | 新 (v2.11.0+) | 旧 (v2.10.x) | ✅ 全カテゴリ同値、旧 UI で編集可 |
| **すべて新版** | 旧形式 (未編集) | 新 (v2.11.0+) | 新 (v2.12.0+) | ✅ 旧形式は legacy 展開で動作、警告 1 回 |

### 既存ユーザーへの推奨アップグレード手順

1. mcp-async-skill を `lazy-v2.11.0` 以降に上書きインストール (worker shutdown 推奨は #62 で扱う)
2. `queue_config.json` の `category_rate_limits` セクションを新形式に書き換え (旧形式のままでも動くが警告ログが出る)
3. 必要に応じて `limits.{cat}` の値をサービス側のレートリミットに合わせて調整

## 設計判断ログ

PR1 で議論し決定した仕様：

- **`defaults` 階層を持たない (limits フラット構造)**: 「全カテゴリのデフォルトを一斉変更」という運用は実態として発生しない。サービス側のレートリミット変更は単一カテゴリだけに来るので、各カテゴリ独立の `limits.{cat}` 構造が直感的。
- **未知カテゴリ (`get_max_inflight("unknown")` 等) はハードコード初期値を返す**: 例外を投げると dispatcher の境界条件で予期せず壊れる。debug log で気づける程度のソフトな扱い。
- **`set_max_inflight(cat, value)` の cat 必須化 (BREAKING)**: lazy-v2.10.x の単一引数 setter は `set_max_inflight(value)` で全カテゴリ一括変更だったが、per-category 化に伴い必ずカテゴリ指定が必要。worker の `PATCH /api/config` ハンドラが「全カテゴリ一括」の API レイヤを emulate する。
- **deprecation log は CategoryLimiter instance ごとに 1 回**: process global one-shot だと複数 worker / テスト環境で 1 回しか見えなくなる。instance ごとのほうが診断に役立つ。
