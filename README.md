# mymind-archive

**English** | [日本語](#日本語)

Keep a local copy of everything you save in [mymind](https://mymind.com), updated daily through the official API.
Items you later delete in mymind stay in your archive.

This is an unofficial tool. It is not affiliated with mymind.

## What it does

- Once a day, fetches the full list of your objects and compares it with the local copy
- Saves new items, and overwrites updated items with their latest state
- Never deletes anything locally. When an item disappears from mymind, it is kept and marked with a deletion date
  (within 30 days of deletion, the script also fetches the item's final state)
- Clears the deletion mark when you restore an item from the trash
- Downloads original files (images, PDFs and so on) and screenshots
- Optionally mirrors the archive one-way into another folder, such as Dropbox

Requires Python 3.9 or later and uses only the standard library. Written for macOS.

Command output and code comments are currently in Japanese.

## Where things live

The code and your data are kept apart. Your data, settings and API key never go into this repository.

| What | Where |
|---|---|
| Code | This repository (clone it anywhere) |
| Settings | `~/.config/mymind-archive/config.json` |
| API key | macOS Keychain |
| Data | `~/mymind-archive` (change with `data_dir`) |
| Mirror | `mirror_dir` in the settings (optional) |
| Log | `~/Library/Logs/mymind-archive/backup.log` |

If `data_dir` or `mirror_dir` points inside this repository, the script stops, so that you cannot commit your data by accident.

## Setup

### 1. Create an API key

Create an access key at <https://access.mymind.com/extensions>. **Read only** access is enough.
You get a key ID and a secret. The secret is shown only once.

### 2. Store the key in the Keychain

```sh
security add-generic-password -U -s mymind-archive -a kid -w
security add-generic-password -U -s mymind-archive -a secret -w
```

Each command asks for the value. Paste it in (it is not shown on screen).

If you are working over SSH, unlock the keychain first.

```sh
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

### 3. Write the settings (optional)

```sh
mkdir -p ~/.config/mymind-archive
cp config.example.json ~/.config/mymind-archive/config.json
```

Set `mirror_dir` to `null` if you do not want a mirror. Without a settings file, the defaults apply.

### 4. Dry run

```sh
./backup.py --probe
```

This writes nothing. It shows whether authentication works, how many items you have, and how many API credits a run costs.

### 5. First run

```sh
./backup.py
```

Text and raw data arrive in a single request. Original files and screenshots are fetched one by one,
so with many items the script may stop when your API credits run low. It picks up where it left off on the next run.

### 6. Run it every day

```sh
./scripts/install-launchd.sh
```

It runs daily at 4:20. To change the time: `MYMIND_ARCHIVE_HOUR=6 MYMIND_ARCHIVE_MINUTE=0 ./scripts/install-launchd.sh`.
To remove the job: `./scripts/uninstall-launchd.sh` (your data stays).

If `mirror_dir` is a cloud storage folder (`~/Library/CloudStorage/...`), macOS may ask for permission the first time.
After installing, run the job once with `launchctl kickstart gui/$(id -u)/local.mymind-archive` and check the screen for a permission dialog.

## What gets saved

```
~/mymind-archive/
  index.jsonl                 one line per item (title, URL, created, modified, deleted, file path)
  deleted.md                  items deleted from mymind, most recent first
  runs.jsonl                  run history
  state.json                  bookkeeping used by the script
  items/2026-11/
    20261120_Title_xxxxxx.md              for reading: tags, spaces, summary, notes, content
    20261120_Title_xxxxxx.screenshot.jpg  screenshot taken when the item was saved
    20261120_Title_xxxxxx.pdf             the original file you uploaded
  raw/
    <ID>.json                 raw object as returned by the API
    <ID>.embeddings.json      the object's vector embeddings from mymind
```

- File names are `date saved_title_end of ID`. They do not change if the title changes later
- Each Markdown file starts with `status: active` or `status: deleted`
- Markdown can be rebuilt from the raw data at any time: `./backup.py --rebuild`

## Configuration

`~/.config/mymind-archive/config.json`

| Key | Default | Meaning |
|---|---|---|
| `data_dir` | `~/mymind-archive` | Where the data is kept |
| `mirror_dir` | `null` | Mirror destination. Files there are never deleted, and files removed there are copied again on the next run |
| `include_embeddings` | `true` | Also save vector embeddings |
| `download_screenshots` | `true` | Save screenshots |
| `download_blobs` | `true` | Save original files |
| `try_direct_media_url` | `true` | Try the media URL directly before going through the API |
| `sustained_reserve_ratio` | `0.3` | When the 30-day credit allowance drops below this share, leave remaining files for the next run |
| `max_blob_mb` | `200` | Skip original files larger than this |
| `mass_delete_guard` | `0.3` | If more than this share of items vanishes from the list at once, assume an API problem and do not mark them deleted |

The environment variables `MYMIND_ARCHIVE_DIR`, `MYMIND_MIRROR_DIR` and `MYMIND_ARCHIVE_CONFIG_DIR` also work.

## Good to know

- **Deletions are detected once per run.** An item saved and deleted between two runs leaves no trace
- **Deleting a lot at once.** If more than 30% of your items (and more than 20 items) disappear at once, they are not marked as deleted, to be safe.
  If you really deleted them, run `./backup.py --accept-mass-delete` once
- **The mirror is not a sync.** It is a one-way copy. The source of truth is `data_dir`
- **Earlier versions are not kept.** Updated items are overwritten. If you need older versions, rely on the version history of wherever you mirror to
- **API credits.** The mymind API gives each plan a credit allowance. To leave room for your other uses,
  this script stops fetching files when the 30-day allowance drops below 30%

### Not yet verified against the live API

A few points could not be settled from the API documentation alone, so the script handles them as follows.
`--probe` shows which case applies.

- Whether the secret is base64-decoded before signing, and whether the `path` claim includes the query string:
  the script tries all four combinations and remembers the one that works
- Whether media URLs can be fetched without authentication:
  the script tries the URL directly first, then falls back to the API
- Credit cost per request: the script adds up the values from the response headers and logs them

## Tests

The tests run the whole flow against a mock API server: save, update, delete, restore, and stop-and-resume.

```sh
python3 -m unittest discover -s tests -v
```

## License and terms

The code is released under the MIT License ([LICENSE](LICENSE)).

The mymind API has its own terms (<https://access.mymind.com/api/terms>).
The API is for personal, non-commercial use only. Follow those terms when you use this tool.

---

# 日本語

[English](#mymind-archive) | **日本語**

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
