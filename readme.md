# yt-dlp + storage.to Web Downloader

[yt-dlp](https://github.com/yt-dlp/yt-dlp) で動画を一時取得し、[storage.to](https://storage.to/) へ自動アップロードして有効期限 **7日間** の共有URLを発行するシンプルな Web アプリケーションです。

## 主な特徴

- **非同期ジョブ & リアルタイム進捗**: `yt-dlp` の取得進捗と `storage.to` へのアップロード進捗・速度をブラウザ上にリアルタイム表示します。
- **単一ジョブ排他制御**: メモリ消費とディスク負荷を抑えるため、1人のダウンロード〜アップロード〜一時ファイル削除〜メモリ解放（`gc.collect()`）が完了するまで他のリクエストを受け付けません（処理中は HTTP `503` を返却）。
- **一時ファイルの即時削除**: `storage.to` へのアップロード完了後（またはエラー発生時）にサーバー上の一時ファイルを即座に削除します。
- **セキュリティ対策**: SSRF対策（プライベートIP・ローカルネットワークへのアクセス遮断）および IP 単位の簡易レート制限を備えています。

## 構成ファイル

- `yt_dlp_web.py` — Flask バックエンド / `yt-dlp` / `storage.to` API 連携
- `index.html` — フロントエンド UI
- `requirements.txt` — Python 依存パッケージ
- `Dockerfile` — `ffmpeg` + `gunicorn` を含む本番用コンテナ定義

## ローカルでの起動方法

事前に **Python 3.11+** と **ffmpeg** をインストールしてください。

```bash
pip install -r requirements.txt
python yt_dlp_web.py
```

起動後、ブラウザで `http://127.0.0.1:5000` を開きます（別ポートにする場合は `PORT=5001` を指定してください）。

## Docker での起動方法

```bash
docker build -t yt-dlp-web .
docker run -p 5000:5000 yt-dlp-web
```

## 環境変数（任意）

| 変数名 | デフォルト | 説明 |
| --- | --- | --- |
| `PORT` | `5000` | 待ち受けポート番号 |
| `STORAGE_TO_API_TOKEN` | 未設定 | `storage.to` の個人 API トークン（未設定時は匿名アップロード） |
| `MAX_FILESIZE_MB` | `500` | 1ファイルあたりの最大サイズ（MB） |
| `RATE_LIMIT_PER_MIN` | `15` | 1IPあたりの1分間リクエスト上限 |
| `DOWNLOAD_DIR` | `./downloads` | 一時ファイルの保存先ディレクトリ |

## 注意事項

- ダウンロードや共有は、対象サイトの利用規約および著作権法を遵守した範囲でご利用ください。
- クラウド環境の IP アドレスからアクセスする場合、動画配信サイト側でボット判定や地域制限を受けることがあります。
