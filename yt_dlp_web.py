"""
yt-dlp + storage.to 連携 オンライン動画ダウンロード Web アプリ (Docker / Gunicorn 専用)

GitHub Pages などの別ドメインに置いた index.html からも通信できるよう CORS に対応しています。
メモリ消費を抑えるため、同時に1人のリクエストのみを受け付けます。
ダウンロード・storage.to (有効期限7日) へのアップロード・一時ファイル削除・
メモリ解放 (gc.collect) が完全に終わるまで、他のユーザーからの新規リクエストは 503 で拒否します。

環境変数 (任意):
    ALLOWED_ORIGIN       : CORS を許可するフロントエンドのURL (例: https://user.github.io / 未設定時は *)
    STORAGE_TOKEN        : storage.to で発行した実際のAPIトークン (未設定時は匿名アップロード)
    MAX_FILESIZE_MB      : 1ファイルあたりの上限サイズ MB (デフォルト: 500)
    RATE_LIMIT_PER_MIN   : 1IPあたりの1分間リクエスト上限 (デフォルト: 15)
    YTDLP_PROXY          : yt-dlp 用プロキシURL (例: http://user:pass@host:port / socks5://host:port)
    YTDLP_PROXY_DOMAINS  : プロキシを適用するドメイン (カンマ区切り、全サイトは * / デフォルト: tver.jp)

起動方法 (Gunicorn / Docker):
    gunicorn -w 1 --threads 8 -b 0.0.0.0:5000 yt_dlp_web:app
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import gc
import ipaddress
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from typing import Callable
from urllib.parse import urlparse
import uuid

from flask import Flask, jsonify, make_response, render_template, request
import requests
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR))

# 環境変数設定
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*").strip() or "*"
STORAGE_TO_BASE = "https://storage.to/api"
STORAGE_TO_API_TOKEN = (
    os.environ.get("STORAGE_TOKEN")
    or os.environ.get("STORAGE_TO_API_TOKEN")
    or ""
).strip()
STORAGE_TO_EXPIRY_DAYS = 7

MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "500"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "15"))
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip(" \"'")
YTDLP_PROXY_DOMAINS = (
    os.environ.get("YTDLP_PROXY_DOMAINS", "").strip(" \"'") or "tver.jp"
)

# 同時実行は必ず1ジョブに制限
executor = ThreadPoolExecutor(max_workers=1)
jobs_lock = threading.Lock()
rate_lock = threading.Lock()
process_lock = threading.Lock()
is_busy = False

# 匿名アップロード時に一貫して使うビジタートークン
DEFAULT_VISITOR_TOKEN = uuid.uuid4().hex


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp
    return None


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@dataclass
class DownloadJob:
    job_id: str
    status: str = "queued"  # queued | downloading | processing | uploading | done | error
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    filename: str = ""
    share_url: str = ""
    expires_at: str = ""
    human_size: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)


JOBS: dict[str, DownloadJob] = {}
RATE_BUCKETS: dict[str, list[float]] = {}


class ProgressFileReader:
    """Content-Length を維持したままアップロード進捗を計測するためのファイルラッパー"""

    def __init__(self, file_obj, total_size: int, callback: Callable[[int], None]):
        self._f = file_obj
        self._total = total_size
        self._callback = callback

    def __len__(self) -> int:
        return self._total

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        if chunk:
            self._callback(len(chunk))
        return chunk


class ProgressBytesReader:
    """マルチパートチャンク用の進捗計測ラッパー"""

    def __init__(self, data: bytes, callback: Callable[[int], None]):
        self._data = data
        self._pos = 0
        self._callback = callback

    def __len__(self) -> int:
        return len(self._data)

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size is None or size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + size]
            self._pos += len(chunk)
        if chunk:
            self._callback(len(chunk))
        return chunk


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    with rate_lock:
        timestamps = [t for t in RATE_BUCKETS.get(ip, []) if now - t < 60]
        if len(timestamps) >= RATE_LIMIT_PER_MIN:
            RATE_BUCKETS[ip] = timestamps
            return False
        timestamps.append(now)
        RATE_BUCKETS[ip] = timestamps
        return True


def is_safe_public_url(url: str) -> tuple[bool, str]:
    """SSRF対策: http/https かつ パブリックIPに解決されるURLのみ許可する"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "URLの形式が不正です。"

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "有効な http/https のURLを入力してください。"

    try:
        addr_infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False, "ホスト名を解決できませんでした。"

    for info in addr_infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, "IPアドレスの検証に失敗しました。"
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
        ):
            return False, "ローカルまたはプライベートネットワークへのアクセスは禁止されています。"

    return True, ""


def apply_proxy_if_needed(ydl_opts: dict, url: str) -> None:
    """YTDLP_PROXY が設定されている場合、対象ドメイン（デフォルト tver.jp）にプロキシを適用する"""
    if not YTDLP_PROXY:
        return
    if YTDLP_PROXY_DOMAINS == "*":
        ydl_opts["proxy"] = YTDLP_PROXY
        return

    host = (urlparse(url).hostname or "").lower()
    domains = [d.strip().lower() for d in YTDLP_PROXY_DOMAINS.split(",") if d.strip()]
    if any(host == d or host.endswith(f".{d}") for d in domains):
        ydl_opts["proxy"] = YTDLP_PROXY


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "video"


def storage_to_headers(owner_token: str = "") -> dict[str, str]:
    headers = {"X-Visitor-Token": DEFAULT_VISITOR_TOKEN}
    if STORAGE_TO_API_TOKEN:
        headers["Authorization"] = f"Bearer {STORAGE_TO_API_TOKEN}"
    elif owner_token:
        headers["Authorization"] = f"Owner {owner_token}"
    if owner_token:
        headers["X-Owner-Token"] = owner_token
    return headers


def safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"storage.to HTTP {resp.status_code}: {resp.text[:200]}")


def upload_to_storage_to(
    job_id: str,
    file_path: Path,
    filename: str,
    expiry_days: int = 7,
) -> dict:
    """
    storage.to REST API (init -> PUT -> confirm -> expiry) でファイルをアップロードする。
    50MB超のファイルは自動的にマルチパートアップロードを実行する。
    """
    file_size = file_path.stat().st_size
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    tag = job_id[:8]
    print(f"[{tag}] storage.to init: {filename} ({file_size} bytes)", flush=True)

    # 1. /upload/init
    init_resp = requests.post(
        f"{STORAGE_TO_BASE}/upload/init",
        headers=storage_to_headers(),
        json={
            "filename": filename[:255],
            "content_type": content_type,
            "size": file_size,
        },
        timeout=(15, 30),
    )
    init_data = safe_json(init_resp)
    if not init_resp.ok or not init_data.get("success"):
        raise RuntimeError(init_data.get("error", f"storage.to init failed ({init_resp.status_code})"))

    r2_key = init_data["r2_key"]
    owner_token = init_data.get("owner_token", "")
    upload_type = init_data.get("type", "single")
    print(f"[{tag}] storage.to type={upload_type}", flush=True)

    uploaded_bytes = 0
    start_time = time.time()

    def on_bytes_sent(n: int) -> None:
        nonlocal uploaded_bytes
        uploaded_bytes += n
        elapsed = max(0.1, time.time() - start_time)
        mb_per_s = (uploaded_bytes / (1024 * 1024)) / elapsed
        pct = round((uploaded_bytes / max(1, file_size)) * 100, 1)
        with jobs_lock:
            j = JOBS.get(job_id)
            if j:
                j.progress = min(99.5, pct)
                j.speed = f"{mb_per_s:.2f} MB/s"

    # 2. PUT bytes to presigned URL(s)
    if upload_type == "single":
        upload_url = init_data["upload_url"]
        with file_path.open("rb") as f:
            reader = ProgressFileReader(f, file_size, on_bytes_sent)
            put_resp = requests.put(
                upload_url,
                data=reader,
                headers={"Content-Type": content_type},
                timeout=(15, 180),
            )
        if not put_resp.ok:
            raise RuntimeError(f"storage.to single PUT failed ({put_resp.status_code}): {put_resp.text[:200]}")

    elif upload_type == "multipart":
        upload_id = init_data["upload_id"]
        part_size = int(init_data["part_size"])
        total_parts = int(init_data["total_parts"])
        part_urls: dict[int, str] = {
            int(k): v for k, v in (init_data.get("initial_urls") or {}).items()
        }

        missing_parts = [p for p in range(1, total_parts + 1) if p not in part_urls]
        if missing_parts:
            parts_resp = requests.post(
                f"{STORAGE_TO_BASE}/upload/parts",
                headers=storage_to_headers(owner_token),
                json={"upload_id": upload_id, "part_numbers": missing_parts},
                timeout=(15, 30),
            )
            parts_data = safe_json(parts_resp)
            if not parts_resp.ok or not parts_data.get("success"):
                raise RuntimeError(parts_data.get("error", "storage.to /upload/parts failed"))
            for item in parts_data.get("part_urls", []):
                part_urls[int(item["partNumber"])] = item["url"]

        uploaded_parts: list[dict] = []
        try:
            with file_path.open("rb") as f:
                for part_num in range(1, total_parts + 1):
                    chunk = f.read(part_size)
                    print(f"[{tag}] uploading part {part_num}/{total_parts}", flush=True)
                    reader = ProgressBytesReader(chunk, on_bytes_sent)
                    put_resp = requests.put(
                        part_urls[part_num],
                        data=reader,
                        timeout=(15, 180),
                    )
                    del chunk, reader
                    if not put_resp.ok:
                        raise RuntimeError(f"Part {part_num} PUT failed ({put_resp.status_code})")
                    etag = put_resp.headers.get("ETag", "")
                    uploaded_parts.append({"partNumber": part_num, "etag": etag})

            comp_resp = requests.post(
                f"{STORAGE_TO_BASE}/upload/complete-multipart",
                headers=storage_to_headers(owner_token),
                json={"upload_id": upload_id, "parts": uploaded_parts},
                timeout=(15, 60),
            )
            comp_data = safe_json(comp_resp)
            if not comp_resp.ok or not comp_data.get("success"):
                raise RuntimeError(comp_data.get("error", "complete-multipart failed"))
        except Exception:
            try:
                requests.post(
                    f"{STORAGE_TO_BASE}/upload/abort",
                    headers=storage_to_headers(owner_token),
                    json={"upload_id": upload_id},
                    timeout=10,
                )
            except Exception:
                pass
            raise
    else:
        raise RuntimeError(f"未対応のアップロード方式です: {upload_type}")

    # 3. /upload/confirm
    print(f"[{tag}] storage.to confirm", flush=True)
    confirm_resp = requests.post(
        f"{STORAGE_TO_BASE}/upload/confirm",
        headers=storage_to_headers(owner_token),
        json={
            "filename": filename[:255],
            "size": file_size,
            "content_type": content_type,
            "r2_key": r2_key,
        },
        timeout=(15, 30),
    )
    confirm_data = safe_json(confirm_resp)
    if not confirm_resp.ok or not confirm_data.get("success"):
        raise RuntimeError(confirm_data.get("error", "storage.to confirm failed"))

    file_info = confirm_data["file"]
    file_id = file_info["id"]
    confirm_owner_token = confirm_data.get("owner_token") or owner_token

    # 4. 有効期限を 7 日に設定 (/file/{id}/expiry)
    print(f"[{tag}] storage.to set expiry={expiry_days}d for {file_id}", flush=True)
    expiry_resp = requests.post(
        f"{STORAGE_TO_BASE}/file/{file_id}/expiry",
        headers=storage_to_headers(confirm_owner_token),
        json={"days": expiry_days},
        timeout=(15, 30),
    )
    if expiry_resp.ok:
        exp_data = safe_json(expiry_resp)
        if exp_data.get("file", {}).get("expires_at"):
            file_info["expires_at"] = exp_data["file"]["expires_at"]

    print(f"[{tag}] storage.to done: {file_info.get('url')}", flush=True)
    return file_info


def run_download_job(job_id: str, url: str, preset: str) -> None:
    global is_busy
    tag = job_id[:8]

    with jobs_lock:
        job = JOBS[job_id]
        job.status = "downloading"

    def progress_hook(d: dict) -> None:
        with jobs_lock:
            j = JOBS.get(job_id)
            if not j:
                return
            st = d.get("status")
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                if total > 0:
                    j.progress = round((downloaded / total) * 100, 1)
                j.speed = d.get("_speed_str", "").strip()
                j.eta = d.get("_eta_str", "").strip()
            elif st == "finished":
                j.status = "processing"
                j.progress = 99.0

    outtmpl = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")
    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "progress_hooks": [progress_hook],
    }
    apply_proxy_if_needed(ydl_opts, url)

    if preset == "best_mp4":
        ydl_opts["format"] = "bv*+ba/b"
        ydl_opts["merge_output_format"] = "mp4"
    elif preset == "1080p":
        ydl_opts["format"] = "bv*[height<=1080]+ba/b[height<=1080]/b"
        ydl_opts["merge_output_format"] = "mp4"
    elif preset == "720p":
        ydl_opts["format"] = "bv*[height<=720]+ba/b[height<=720]/b"
        ydl_opts["merge_output_format"] = "mp4"
    elif preset == "audio_mp3":
        ydl_opts["format"] = "ba/b"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif preset == "audio_m4a":
        ydl_opts["format"] = "ba[ext=m4a]/ba/b"
    else:
        with jobs_lock:
            job.status = "error"
            job.error = "不正なフォーマット指定です。"
        with process_lock:
            is_busy = False
        return

    if preset in ("best_mp4", "1080p", "720p", "audio_mp3") and not shutil.which("ffmpeg"):
        print(f"[{tag}] WARNING: ffmpeg not found in PATH! Video+audio merging may fail.", flush=True)

    target_path: Path | None = None
    info = None
    uploaded = None
    try:
        print(
            f"[{tag}] yt-dlp start: {url} ({preset}) proxy_applied={bool(ydl_opts.get('proxy'))} domains={YTDLP_PROXY_DOMAINS}",
            flush=True,
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            print(
                f"[{tag}] selected format={info.get('format')} vcodec={info.get('vcodec')} acodec={info.get('acodec')}",
                flush=True,
            )
            prepared = Path(ydl.prepare_filename(info))
        info = None

        matched = list(DOWNLOAD_DIR.glob(f"{job_id}_*"))
        target_path = matched[0] if matched else prepared
        if not target_path.exists():
            raise FileNotFoundError("ダウンロード後のファイルが見つかりませんでした。")

        clean_name = sanitize_filename(target_path.name.removeprefix(f"{job_id}_"))

        with jobs_lock:
            job.status = "uploading"
            job.progress = 0.0
            job.speed = ""
            job.eta = ""
            job.filename = clean_name

        uploaded = upload_to_storage_to(
            job_id,
            target_path,
            clean_name,
            expiry_days=STORAGE_TO_EXPIRY_DAYS,
        )

        with jobs_lock:
            job.status = "done"
            job.progress = 100.0
            job.speed = ""
            job.share_url = uploaded.get("url", "")
            job.expires_at = uploaded.get("expires_at", "")
            job.human_size = uploaded.get("human_size", "")
    except Exception as exc:
        print(f"[{tag}] error: {exc}", flush=True)
        with jobs_lock:
            job.status = "error"
            job.error = f"処理に失敗しました: {exc}"
    finally:
        # 1. ローカルの一時ファイルを完全に削除
        for leftover in DOWNLOAD_DIR.glob(f"{job_id}_*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        if target_path and target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                pass

        # 2. 参照を破棄し、ガベージコレクションでメモリを解放
        del info, uploaded, target_path
        with jobs_lock:
            old_ids = [jid for jid in JOBS if jid != job_id]
            for jid in old_ids:
                JOBS.pop(jid, None)
        gc.collect()

        # 3. メモリ解放が終わってから次のユーザーのリクエストを受け付ける
        with process_lock:
            is_busy = False
        print(f"[{tag}] cleanup complete, ready for next request", flush=True)


@app.get("/")
def index():
    return render_template(
        "index.html",
        max_mb=MAX_FILESIZE_MB,
        expiry_days=STORAGE_TO_EXPIRY_DAYS,
    )


@app.get("/healthz")
def healthz():
    with process_lock:
        busy = is_busy
    return jsonify({"status": "ok", "busy": busy})


@app.post("/api/info")
def get_video_info():
    with process_lock:
        if is_busy:
            return jsonify({"error": "現在ほかのユーザーが処理中です。完了とメモリ解放までお待ちください。"}), 503

    if not check_rate_limit(get_client_ip()):
        return jsonify({"error": "リクエストが多すぎます。少し待ってから再試行してください。"}), 429

    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    ok, err = is_safe_public_url(url)
    if not ok:
        return jsonify({"error": err}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    apply_proxy_if_needed(ydl_opts, url)
    print(f"[info] url={url} proxy_applied={bool(ydl_opts.get('proxy'))} domains={YTDLP_PROXY_DOMAINS}", flush=True)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        res = {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
        }
        del info
        gc.collect()
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": f"動画情報の取得に失敗しました: {exc}"}), 400


@app.post("/api/download")
def start_download():
    global is_busy

    if not check_rate_limit(get_client_ip()):
        return jsonify({"error": "リクエストが多すぎます。少し待ってから再試行してください。"}), 429

    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    preset = payload.get("preset", "best_mp4")

    ok, err = is_safe_public_url(url)
    if not ok:
        return jsonify({"error": err}), 400

    with process_lock:
        if is_busy:
            return jsonify({"error": "現在ほかのユーザーが処理中です。完了とメモリ解放までお待ちください。"}), 503
        is_busy = True

    job_id = uuid.uuid4().hex
    with jobs_lock:
        JOBS[job_id] = DownloadJob(job_id=job_id)

    try:
        executor.submit(run_download_job, job_id, url, preset)
    except Exception as exc:
        with process_lock:
            is_busy = False
        return jsonify({"error": f"ジョブの開始に失敗しました: {exc}"}), 500

    return jsonify({"job_id": job_id}), 202


@app.get("/api/status/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "ジョブが見つかりません。"}), 404
        return jsonify({
            "job_id": job.job_id,
            "status": job.status,
            "progress": job.progress,
            "speed": job.speed,
            "eta": job.eta,
            "filename": job.filename,
            "share_url": job.share_url,
            "expires_at": job.expires_at,
            "human_size": job.human_size,
            "error": job.error,
        })
