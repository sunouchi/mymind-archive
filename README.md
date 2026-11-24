# mymind-archive

[mymind](https://mymind.com) に保存したものを、公式 API で毎日手元に写していくスクリプトです。
mymind で消したものも、手元には残ります。

非公式のツールです。mymind とは関係ありません。

## 何をするか

- 毎日 1 回、全件の一覧を取って手元と比べる
- 新しいものは保存する。更新されたものは最新の状態で上書きする
- mymind から消えたものは、手元からは消さずに「削除日」を書き足す
  （消してから 30 日以内なら、消える直前の最後の状態を取りに行く）
- ゴミ箱から戻したものは、削除の印を外す
- 画像・PDF などの原本とスクリーンショットも保存する
- 指定したフォルダ（Dropbox など）へ一方通行で写しを置く

Python 3.9 以上、標準ライブラリだけで動きます。macOS を想定しています。

## 置き場所

コードと、あなたのデータは別の場所に置きます。データ・設定・API キーはこのリポジトリに入りません。

| もの | 場所 |
|---|---|
| コード | このリポジトリ（どこに clone してもよい） |
| 設定 | `~/.config/mymind-archive/config.json` |
| API キー | macOS のキーチェーン |
| データ | `~/mymind-archive`（設定の `data_dir` で変更可） |
| 写し | 設定の `mirror_dir`（任意） |
| ログ | `~/Library/Logs/mymind-archive/backup.log` |

`data_dir` や `mirror_dir` がこのリポジトリの中を指していると、誤ってコミットしないようスクリプトは止まります。

## セットアップ

### 1. API キーを作る

<https://access.mymind.com/extensions> でアクセスキーを作ります。権限は **Read only** で足ります。
キー ID とシークレットが表示されます。シークレットは一度しか表示されません。

### 2. キーチェーンに入れる

```sh
security add-generic-password -U -s mymind-archive -a kid -w
security add-generic-password -U -s mymind-archive -a secret -w
```

それぞれ値を聞かれるので貼り付けます（画面には表示されません）。

SSH 越しに作業している場合は、先にキーチェーンのロックを外します。

```sh
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

### 3. 設定を書く（任意）

```sh
mkdir -p ~/.config/mymind-archive
cp config.example.json ~/.config/mymind-archive/config.json
```

写しが要らなければ `mirror_dir` を `null` にします。設定ファイルがなくても既定値で動きます。

### 4. お試し

```sh
./backup.py --probe
```

何も書き込まずに、認証が通るか、何件あるか、API クレジットをどれだけ使うかを表示します。

### 5. 1 回目を動かす

```sh
./backup.py
```

文章と生データは 1 回で全部そろいます。画像・原本は 1 件ずつ取るので、件数が多いと
クレジットの残りを見て途中で止まります。続きは次の回に自動で再開します。

### 6. 毎日動かす

```sh
./scripts/install-launchd.sh
```

毎日 4:20 に動きます。時刻を変えるなら `MYMIND_ARCHIVE_HOUR=6 MYMIND_ARCHIVE_MINUTE=0 ./scripts/install-launchd.sh`。
外すときは `./scripts/uninstall-launchd.sh`（データは残ります）。

`mirror_dir` がクラウドストレージのフォルダ（`~/Library/CloudStorage/...`）の場合、初回に macOS が
アクセス許可を聞いてくることがあります。登録したあと一度 `launchctl kickstart gui/$(id -u)/local.mymind-archive`
で動かして、画面に許可のダイアログが出ていないか確かめてください。

## 保存される形

```
~/mymind-archive/
  index.jsonl                 1 行 1 件（タイトル、URL、保存日、更新日、削除日、ファイルの場所）
  deleted.md                  mymind から消したものの一覧（新しく消した順）
  runs.jsonl                  実行の記録
  state.json                  スクリプトが使う管理情報
  items/2026-11/
    20261120_タイトル_xxxxxx.md             読む用。タグ、スペース、要約、メモ、本文
    20261120_タイトル_xxxxxx.screenshot.jpg  保存時のスクリーンショット
    20261120_タイトル_xxxxxx.pdf             アップロードした原本
  raw/
    <ID>.json                 API が返した生データ
    <ID>.embeddings.json      mymind が持っているベクトル
```

- ファイル名は「保存日_タイトル_ID の末尾」。あとでタイトルが変わってもファイル名は変えません
- Markdown の先頭に `status: active` か `status: deleted` が入ります
- 生データがあれば Markdown はいつでも作り直せます: `./backup.py --rebuild`

## 設定

`~/.config/mymind-archive/config.json`

| キー | 既定値 | 意味 |
|---|---|---|
| `data_dir` | `~/mymind-archive` | データの置き場 |
| `mirror_dir` | `null` | 写し先。写し側のファイルは消さない。写し側で消えたものは次回また写す |
| `include_embeddings` | `true` | ベクトルも保存する |
| `download_screenshots` | `true` | スクリーンショットを保存する |
| `download_blobs` | `true` | 原本ファイルを保存する |
| `try_direct_media_url` | `true` | 画像の URL が直接取れるなら先にそちらを試す |
| `sustained_reserve_ratio` | `0.3` | 30 日枠のクレジットがこの割合を切ったら、画像・原本の取得を次回に回す |
| `max_blob_mb` | `200` | これより大きい原本は取らない |
| `mass_delete_guard` | `0.3` | 一度にこの割合を超えて一覧から消えたら、API の不調を疑って削除扱いにしない |

環境変数 `MYMIND_ARCHIVE_DIR` `MYMIND_MIRROR_DIR` `MYMIND_ARCHIVE_CONFIG_DIR` でも指定できます。

## 知っておくこと

- **削除の検知は実行の間隔に依存します。** 保存してから次の実行までに消したものは、記録に残りません
- **大量に消したとき。** 一度に 3 割を超えて（かつ 20 件を超えて）消えていると、安全のため削除扱いにしません。
  本当に消したのなら `./backup.py --accept-mass-delete` を一度動かしてください
- **写しは同期ではありません。** 一方通行のコピーです。原本は `data_dir` 側です
- **更新前の版は残りません。** 上書きします。前の版が要るなら、写し先のサービスの版履歴に頼ることになります
- **API クレジット。** mymind の API はプランごとにクレジットの枠があります。このスクリプトは
  ほかの用途の分を残すよう、30 日枠が 3 割を切ると画像・原本の取得をやめます

### まだ実物で確かめていない点

mymind の API ドキュメントだけでは決めきれなかったところがあり、次のように作ってあります。
`--probe` の結果を見れば、どれに当たったかが分かります。

- 署名に使うシークレットを base64 デコードするかどうか、`path` クレームにクエリを含めるかどうか
  → 4 通りを順に試し、通った方式を覚えます
- 画像の URL が認証なしで取れるかどうか → 先に直接試し、だめなら API 経由で取ります
- 1 リクエストあたりの消費クレジット → 応答ヘッダーの値を集計してログに出します

## テスト

作り物の API サーバーを相手に、保存・更新・削除・復活・中断と再開を通しで確かめます。

```sh
python3 -m unittest discover -s tests -v
```

## ライセンスと利用条件

コードは MIT ライセンスです（[LICENSE](LICENSE)）。

mymind の API には mymind 側の利用規約があります（<https://access.mymind.com/api/terms>）。
API は個人的・非商用の利用に限られています。このツールを使うときは、そちらにも従ってください。
