# 引き継ぎ文書 (Maintenance Handover)

本リポジトリ **LazyKamuiCodeSkillsCreator** はメンテナンスを終了しました。
この文書は、本プロジェクトを **フォークして継続したい開発者**、および
**後継のツールカタログ (`mcp_tool_catalog.yaml`) をホストするメンテナー** に向けた引き継ぎ事項をまとめたものです。

> エンドユーザー向けの簡易案内は [README のメンテナンス終了のお知らせ](../README.md#-メンテナンス終了のお知らせ) を参照してください。

---

## 1. メンテ終了で何が起きるか

このツールは「MCPサーバーのツール定義」を **外部カタログ `mcp_tool_catalog.yaml`** から取得してスキルを生成します。
カタログ本体は **本リポジトリに同梱しておらず**、生成のたびに `generate_skill.py` 内の
`CATALOG_URL` 定数が指すURLからHTTP取得します（具体的なURLはコード上の定数を参照してください）。

このカタログは [Yumeno/kamuicode-config-manager](https://github.com/Yumeno/kamuicode-config-manager)
リポジトリで生成・配信していました（同リポジトリで更新終了を案内済み）。

このカタログの更新が終了したため：

- 既定URLは（生きている限り）**古いスナップショットを返す** → 新しいMCPサーバー/ツールは生成できない
- 元リポジトリが削除・非公開化されると **404 になり、`--catalog-url` 未指定の生成が即エラー** になる
  （`fetch_catalog()` は失敗時に `sys.exit(1)` する。ローカルfallbackは実装していない）

本体コードは安定しており、**カタログさえ供給できれば生成機能は引き続き動作します。**

---

## 2. カタログURLはどこで定義・参照されているか

| 場所 | 役割 |
|------|------|
| `.claude/skills/mcp-async-skill/scripts/generate_skill.py` の `CATALOG_URL` 定数 | デフォルトURLの**唯一の本体定義**（ここを書き換えれば既定が変わる） |
| 同 `fetch_catalog()` | `requests.get(catalog_url)` でHTTP取得しYAMLパース |
| 同 CLI `--catalog-url` | 実行時にURLを上書き（デフォルトは `CATALOG_URL`） |
| `.claude/skills/mcp-async-skill/SKILL.md` | ドキュメント中にURLを明記（コード変更時はここも追従が必要） |
| `README.md` / `docs/schema-passthrough.md` | 「`mcp_tool_catalog.yaml` から自動取得」と説明（カタログ名のみ） |

後継URLに恒久的に切り替えたい場合、最小の変更は **`generate_skill.py` の `CATALOG_URL` 定数を書き換える**ことです。
あわせて `SKILL.md` 内のURL記載も追従してください。具体的な既定URLは本文に転記せず、常にコード上の定数を正本としています。

---

## 3. 後継カタログをホストするメンテナー向け：必須スキーマ

`generate_skill.py` がカタログに対して**実際にアクセスするフィールド**は以下です。
これらを満たす YAML を raw テキストで返せれば、URL/ホスティング方法は問いません
（GitHub raw でなくとも、任意のHTTPSエンドポイントやローカルHTTPでも可）。

```yaml
metadata:
  total_servers: 123          # int。起動ログ表示に使用（fetch_catalog）

servers:
  - id: <server-id>           # str。--server / 部分一致検索のキー
    status: online            # str。"error" の場合は警告（生成は続行）
    error_message: <任意>     # status が error のとき表示に使用
    tools:
      - name: <tool-name>     # str。submit/status/result パターン推定に使用
        description: <説明>   # str
        inputSchema:          # JSON Schema (object)
          type: object
          required: [<必須プロパティ名>, ...]
          properties:
            <param>:
              type: string | number | integer | boolean | array | object
              description: <説明>
              default: <任意>
              enum: [<任意>]
              minimum: <任意>
              maximum: <任意>
              items:          # type: array のとき
                enum: [<任意>]
```

ポイント：

- **endpoint URL（MCPサーバーの接続先）はカタログには不要**。それはユーザーの `.mcp.json`
  （`url` または `endpoint` キー）から供給される。カタログが持つのは「ツール定義」のみ。
- `inputSchema` の `enum`/`default`/`minimum`/`maximum`/`items.enum` は
  スキーマパススルー機能（[docs/schema-passthrough.md](schema-passthrough.md)）でそのまま生成物に反映される。
  省略しても動くが、付けるほど生成スキルの品質が上がる。
- submit/status/result の3ツールは **ツール名のキーワード**から推定される
  （status側キーワード: `status`/`check`/`poll`/`state`/`progress` 等）。
  命名がこの推定に沿っていると自動判別が効く。

実データ例は [docs/schema-passthrough.md の「実際のカタログデータ例」](schema-passthrough.md) を参照。

---

## 4. ユーザーが後継URL／ローカルカタログを使う方法

コード変更なしで切り替えられます。

**後継URLを使う：**

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --catalog-url https://<後継ホスト>/mcp_tool_catalog.yaml
```

**ローカルに保存したカタログを使う（推奨フォールバック）：**

`fetch_catalog()` は `requests.get` を使うHTTP前提のため、ローカルファイルを直接渡すには
簡易HTTPサーバー経由にするか、コードを `file://`/ローカルパス対応に小改修する必要があります。
恒久運用するフォーク者は、`fetch_catalog()` に「URLが存在しなければ同梱スナップショットを読む」
フォールバックを追加することを推奨します（元の挙動は失敗時 `sys.exit(1)`）。

> 元URLがまだ生きているうちに `mcp_tool_catalog.yaml` を取得して保管しておくと、
> 後継が現れるまでの凍結運用に使えます。

---

## 5. フォークして継続する場合のチェックリスト

- [ ] カタログの供給元を決める（後継URL or 自前ホスト or 同梱スナップショット）
- [ ] `generate_skill.py` の `CATALOG_URL` を新URLに更新（恒久切替の場合）
- [ ] `SKILL.md` / `README.md` 内のURL記載を更新
- [ ] 必要なら `fetch_catalog()` にローカルフォールバックを追加
- [ ] このメンテ終了告知を、新メンテナーの状況に合わせて改稿 or 撤去

---

## 6. 関連ドキュメント

- [README.md](../README.md) — 全体概要・機能一覧
- [docs/schema-passthrough.md](schema-passthrough.md) — カタログ→生成物のスキーマ変換詳細
- [docs/lazy-mode.md](lazy-mode.md) — Lazyモード / 複数サーバー生成
- [docs/output-path-strategy.md](output-path-strategy.md) — 出力パス戦略
