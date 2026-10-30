import os
import glob
import datetime
import requests

# 1. 環境変数から動画のURLを取得
video_url = os.environ.get("VIDEO_URL")
if not video_url:
    print("エラー: VIDEO_URL が設定されていません。")
    exit(1)

# 2. 動画のダウンロード処理を実行
print("🎬 動画のダウンロードを開始します...")
os.makedirs("output", exist_ok=True)
os.system(f'yt-dlp -f "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]" -o "output/%(title)s.%(ext)s" "{video_url}"')

# 3. ダウンロードしたファイルを storage.to へアップロード
files = glob.glob('output/*')
if not files:
    print("エラー: ダウンロードされた動画ファイルが見つかりません。")
    exit(1)

video_path = files[0]
filename = os.path.basename(video_path)
video_title = os.path.splitext(filename)[0]

print(f"📦 storage.to へアップロード中: {filename}")
upload_url = "https://storage.to"

try:
    with open(video_path, 'rb') as f:
        response = requests.post(upload_url, files={'file': (filename, f)})
    
    if response.status_code == 200:
        result = response.json()
        storage_url = result.get("url")
        print(f"🎉 アップロード成功！ URL: {storage_url}")
        
        # 4. 自分専用の履歴ファイル (history.txt) に自動追記
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{now}] {video_title} -> {storage_url}\n"
        
        with open("history.txt", "a", encoding="utf-8") as log_file:
            log_file.write(log_line)
        print("📝 history.txt にダウンロードURLを記録しました。")
        
    else:
        print(f"❌ アップロード失敗 (HTTP {response.status_code})")
        exit(1)
except Exception as e:
    print(f"❌ 通信エラーが発生しました: {e}")
    exit(1)
