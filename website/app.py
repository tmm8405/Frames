import base64
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import sqlite3
import struct
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from urllib.parse import quote

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, session, stream_with_context)
from requests.adapters import HTTPAdapter
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from PIL import Image, ImageOps  # thumbnails for the vault grid
except ImportError:  # pragma: no cover
    Image = ImageOps = None

try:
    import segno  # QR codes for authenticator setup (optional, falls back to typed key)
except ImportError:  # pragma: no cover
    segno = None

load_dotenv()


def env(name, default=""):
    return os.getenv(name, default).strip()


def flag(name, default="1"):
    return env(name, default).lower() not in ("0", "false", "no", "off", "")


BOT_TOKEN = os.environ["BOT_TOKEN"]

# Section 1: private uploads (never shared, no links)
CHAT_ID = os.environ["CHAT_ID"]
ACCESS_CODE = env("ACCESS_CODE")
MAX_MB = int(env("MAX_MB", "50"))

# Section 2: public sharing (separate channel, download links)
SHARE_CHAT_ID = env("SHARE_CHAT_ID")
SHARE_ACCESS_CODE = env("SHARE_ACCESS_CODE")
# Telegram bots can only DOWNLOAD files up to 20 MB, so shared files are capped there
SHARE_MAX_MB = min(int(env("SHARE_MAX_MB", "20")), 20)
BASE_URL = env("BASE_URL").rstrip("/")

# Vault (file manager for the private channel, protected by an authenticator app)
VAULT_SETUP_KEY = env("VAULT_SETUP_KEY")
VAULT_SESSION_HOURS = int(env("VAULT_SESSION_HOURS", "12"))
VAULT_RESET_2FA = flag("VAULT_RESET_2FA", "0")
VAULT_IDLE_MINUTES = int(env("VAULT_IDLE_MINUTES", "5"))  # 0 = never auto-close

# Wallpapers (public JSON + images for the Android app, panel protected by its own authenticator)
WALL_CHAT_ID = env("WALL_CHAT_ID")
WALL_ENABLED = bool(WALL_CHAT_ID)
WALL_SETUP_KEY = env("WALL_SETUP_KEY")
WALL_RESET_2FA = flag("WALL_RESET_2FA", "0")
WALL_MAX_MB = min(int(env("WALL_MAX_MB", "20")), 20)  # bots can only download up to 20 MB
WALL_THUMB_PX = int(env("WALL_THUMB_PX", "480"))
WALL_JSON_PATH = env("WALL_JSON_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "frames.json")

PARALLEL = max(1, int(env("PARALLEL", "3")))

UPLOAD_DEFAULT_CONFIG = {
    "enabled": False,
    "interval_seconds": 900,
    "start_hour": 8,
    "end_hour": 22,
    "wifi_only": False,
    "folders": ["DCIM/Camera/"],
    "extensions": ["jpg", "jpeg", "png", "webp"],
    "upload_existing": True,
    "min_age_seconds": 30,
    "max_bytes": 50 * 1024 * 1024,
    "upload_mode": "normal",
    "slow_delay_seconds": 30,
}

# Database: a single SQLite file (shares.db next to app.py unless DB_PATH says otherwise)
DB_PATH = env("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "shares.db")

# Encryption
ENCRYPT_FILES = flag("ENCRYPT_FILES", "1")
THUMB_PX = 320
# Telegram bots can only DOWNLOAD files up to 20 MB, so the vault can't show bigger images.
# Those images stay in the Telegram channel but are left out of the vault (small margin for the encryption overhead).
VAULT_MAX_BYTES = 20 * 1024 * 1024 - 4096
CHUNK = 1024 * 1024
MAGIC = b"TGE1"
HEADER_LEN = 24  # magic(4) + salt(16) + chunk size(4)


def _load_master_key():
    raw = env("ENCRYPTION_KEY")
    if not raw:
        return None
    try:
        k = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(k) == 32:
            return k
    except Exception:
        pass
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return bytes.fromhex(raw)
    raise RuntimeError("ENCRYPTION_KEY is not a valid 32-byte key (base64url or 64 hex characters).")


MASTER_KEY = _load_master_key()
if ENCRYPT_FILES and not MASTER_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY is not set. Add this line to your .env (and BACK IT UP somewhere safe, "
        "files can't be opened without it):\n\nENCRYPTION_KEY="
        + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + "\n"
    )

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = (max(MAX_MB, SHARE_MAX_MB, WALL_MAX_MB) + 1) * 1024 * 1024
app.config.update(
    SESSION_COOKIE_NAME="vault_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=VAULT_SESSION_HOURS),
)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

session_http = requests.Session()
session_http.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))

IMAGE_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif",
}
IMAGE_MIMES = set(IMAGE_EXT.values())  # SVG is left out on purpose (it can carry scripts)


# ---------- encryption (AES-256-GCM, chunked so big files never sit fully in memory) ----------

class DecryptError(Exception):
    pass


def file_key(salt):
    return hmac.new(MASTER_KEY, b"tg-file-v1" + salt, hashlib.sha256).digest()


def encrypt_to_tmp(src):
    """Encrypt a readable stream into a temp file (RAM up to 8 MB, then disk). Returns it at position 0."""
    salt = os.urandom(16)
    header = MAGIC + salt + struct.pack(">I", CHUNK)
    aes = AESGCM(file_key(salt))
    out = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    out.write(header)
    nxt = src.read(CHUNK)
    counter = 0
    while True:
        cur, nxt = nxt, src.read(CHUNK)
        last = not nxt
        out.write(aes.encrypt(counter.to_bytes(12, "big"), cur, header + (b"\x01" if last else b"\x00")))
        counter += 1
        if last:
            break
    out.seek(0)
    return out


def encrypt_bytes(data):
    tmp = encrypt_to_tmp(io.BytesIO(data))
    try:
        return tmp.read()
    finally:
        tmp.close()


def decrypt_iter(pieces):
    """Decrypt a stream of arbitrary-sized byte pieces, yielding plaintext chunk by chunk."""
    if not MASTER_KEY:
        raise DecryptError("No ENCRYPTION_KEY configured.")
    it = iter(pieces)
    buf = bytearray()

    def fill(n):
        while len(buf) < n:
            try:
                buf.extend(next(it))
            except StopIteration:
                break

    fill(HEADER_LEN)
    header = bytes(buf[:HEADER_LEN])
    if len(header) < HEADER_LEN or header[:4] != MAGIC:
        raise DecryptError("Not an encrypted file.")
    del buf[:HEADER_LEN]
    chunk = struct.unpack(">I", header[20:24])[0]
    if not 1024 <= chunk <= 16 * 1024 * 1024:
        raise DecryptError("Corrupt header.")
    aes = AESGCM(file_key(header[4:20]))
    block_len, counter = chunk + 16, 0
    while True:
        fill(block_len + 1)  # one extra byte tells us whether this is the final chunk
        if not buf:
            raise DecryptError("File is truncated.")
        last = len(buf) <= block_len
        block = bytes(buf[:block_len])
        del buf[:block_len]
        try:
            yield aes.decrypt(counter.to_bytes(12, "big"), block, header + (b"\x01" if last else b"\x00"))
        except InvalidTag:
            raise DecryptError("Wrong key or damaged file.")
        counter += 1
        if last:
            return


def decrypt_bytes(blob):
    return b"".join(decrypt_iter([blob]))


def make_thumb(stream, px=None):
    """Small JPEG preview for the vault grid. Returns bytes or None."""
    if Image is None:
        return None
    px = px or THUMB_PX
    try:
        stream.seek(0)
        Image.MAX_IMAGE_PIXELS = 60_000_000
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(stream)
            img.draft("RGB", (px * 2, px * 2))  # fast path for big JPEGs
            img = ImageOps.exif_transpose(img)
            img.thumbnail((px, px))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            else:
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=80, optimize=True)
            return out.getvalue()
    except Exception:
        return None
    finally:
        stream.seek(0)


# ---------- database (SQLite) ----------

def _connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def conn():
    c = _connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def q_all(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q_one(sql, args=()):
    rows = q_all(sql, args)
    return rows[0] if rows else None


def q_exec(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid


DDL = [
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS shares (
        token TEXT PRIMARY KEY,
        file_id TEXT NOT NULL,
        name TEXT NOT NULL,
        size INTEGER NOT NULL,
        enc INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS private_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        thumb_id TEXT,
        thumb BLOB,
        name TEXT NOT NULL,
        size INTEGER NOT NULL,
        mime TEXT NOT NULL DEFAULT '',
        is_image INTEGER NOT NULL DEFAULT 0,
        enc INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS folder_files (
        folder_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        added REAL NOT NULL,
        PRIMARY KEY (folder_id, file_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ff_file ON folder_files (file_id)",
    "CREATE INDEX IF NOT EXISTS idx_img ON private_files (is_image, id)",
    """CREATE TABLE IF NOT EXISTS wallpapers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL UNIQUE,
        message_id INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        name TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT '',
        copyright TEXT NOT NULL DEFAULT '',
        collections TEXT NOT NULL DEFAULT '',
        featured INTEGER NOT NULL DEFAULT 0,
        downloadable INTEGER NOT NULL DEFAULT 1,
        width INTEGER NOT NULL, height INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mime TEXT NOT NULL, ext TEXT NOT NULL,
        thumb BLOB,
        created REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS upload_devices (
        device_id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        app_version TEXT NOT NULL DEFAULT '',
        sdk INTEGER NOT NULL DEFAULT 0,
        config TEXT NOT NULL,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL
    )""",
]
# Columns added in newer versions; added to an existing shares.db automatically.
# Old rows get enc=0, so files uploaded before encryption still download normally.
UPGRADES = [
    ("shares", "enc", "INTEGER NOT NULL DEFAULT 0"),
    ("private_files", "thumb", "BLOB"),
    ("private_files", "enc", "INTEGER NOT NULL DEFAULT 0"),
    ("private_files", "caption", "TEXT NOT NULL DEFAULT ''"),
    ("private_files", "fav", "INTEGER NOT NULL DEFAULT 0"),
    ("private_files", "ignored", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db():
    with conn() as c:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        for stmt in DDL:
            c.execute(stmt)
        for table, col, decl in UPGRADES:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


init_db()


def get_setting(key, default=None):
    row = q_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def upload_config(device):
    config = dict(UPLOAD_DEFAULT_CONFIG)
    try:
        config.update(json.loads(device["config"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        pass
    return config


def clean_upload_config(raw):
    if not isinstance(raw, dict):
        raise ValueError
    config = {
        "enabled": bool(raw["enabled"]),
        "interval_seconds": max(30, int(raw["interval_seconds"])),
        "start_hour": int(raw["start_hour"]),
        "end_hour": int(raw["end_hour"]),
        "wifi_only": bool(raw["wifi_only"]),
        "folders": [str(v).strip() for v in raw["folders"] if str(v).strip()],
        "extensions": [str(v).strip().lower().lstrip(".") for v in raw["extensions"] if str(v).strip()],
        "upload_existing": bool(raw["upload_existing"]),
        "min_age_seconds": max(0, int(raw["min_age_seconds"])),
        "max_bytes": max(1, int(raw["max_bytes"])),
        "upload_mode": "slow" if raw.get("upload_mode") == "slow" else "normal",
        "slow_delay_seconds": max(1, int(raw.get("slow_delay_seconds", 30))),
    }
    if not 0 <= config["start_hour"] <= 23 or not 0 <= config["end_hour"] <= 23:
        raise ValueError
    if not config["folders"] or not config["extensions"]:
        raise ValueError
    return config


def set_setting(key, value):
    if value is None:
        q_exec("DELETE FROM settings WHERE key = ?", (key,))
    else:
        q_exec("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


if VAULT_RESET_2FA:
    for _k in ("totp_secret", "pending_totp", "last_step", "auth_gen"):
        set_setting(_k, None)

if WALL_RESET_2FA:
    for _k in ("wall_totp_secret", "wall_pending_totp", "wall_last_step", "wall_auth_gen"):
        set_setting(_k, None)

_secret = env("SECRET_KEY") or get_setting("secret_key")
if not _secret:
    _secret = secrets.token_hex(32)
    set_setting("secret_key", _secret)
app.secret_key = _secret


# ---------- helpers ----------

def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


app.jinja_env.filters["size"] = human_size


def fail(msg, code, **extra):
    return jsonify(ok=False, error=msg, **extra), code


def code_ok(expected, given):
    if not expected:
        return True
    return secrets.compare_digest(expected.encode(), (given or "").strip().encode())


def base_url():
    return BASE_URL or request.host_url.rstrip("/")


def image_mime(declared, name):
    m = (declared or "").lower().split(";")[0].strip()
    if m in IMAGE_MIMES:
        return m
    return IMAGE_EXT.get(os.path.splitext(name)[1].lower(), "")


def attachment_header(name):
    ascii_name = name.encode("ascii", "ignore").decode().replace('"', "") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def read_upload(limit_mb):
    file = request.files.get("file")
    if not file or not file.filename:
        return None, None, 0, ("No file received.", 400)
    name = os.path.basename(file.filename.replace("\\", "/"))
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return None, None, 0, ("The file is empty.", 400)
    if size > limit_mb * 1024 * 1024:
        return None, None, 0, (f"File is larger than {limit_mb} MB.", 413)
    return file, name, size, None


def send_document(chat_id, stream, send_name, caption, mimetype):
    """Send to Telegram. Returns (result, error) where error is (msg, status)."""
    data = {}
    for attempt in range(4):
        try:
            stream.seek(0)
            resp = session_http.post(
                f"{API}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (send_name, stream, mimetype)},
                timeout=600,
            )
            data = resp.json()
        except requests.RequestException:
            if attempt == 3:
                return None, ("Could not reach Telegram. Try again.", 502)
            time.sleep(1 + attempt)
            continue
        except ValueError:
            return None, ("Telegram sent an unexpected reply.", 502)

        if data.get("error_code") == 429 and attempt < 3:
            wait = data.get("parameters", {}).get("retry_after", 3)
            time.sleep(min(wait, 30) + 0.5)
            continue
        break

    if not data.get("ok"):
        return None, (data.get("description", "Telegram rejected the file."), 502)
    return data["result"], None


def open_telegram_file(file_id):
    """Open a stream to a file stored on Telegram. Returns (upstream, error)."""
    try:
        info = session_http.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30).json()
    except (requests.RequestException, ValueError):
        return None, ("Telegram is not responding. Try again in a moment.", 502)
    if not info.get("ok"):
        if "too big" in str(info.get("description", "")).lower():
            return None, ("Too large for the bot to fetch (Telegram's 20 MB limit).", 413)
        return None, ("This file can't be fetched anymore.", 410)
    try:
        upstream = session_http.get(
            f"{FILE_API}/{info['result']['file_path']}", stream=True, timeout=(10, 60)
        )
    except requests.RequestException:
        return None, ("Telegram is not responding. Try again in a moment.", 502)
    if upstream.status_code != 200:
        upstream.close()
        return None, ("This file can't be fetched anymore.", 410)
    return upstream, None


def stream_response(upstream, ctype, headers, decrypt_len=None):
    """Stream a Telegram file to the visitor. decrypt_len=<plaintext size> decrypts on the fly."""
    h = dict(headers)
    source = upstream.iter_content(64 * 1024)
    first = None
    if decrypt_len is not None:
        source = decrypt_iter(source)
        try:
            first = next(source)  # surface a wrong key / damaged file BEFORE sending headers
        except (DecryptError, StopIteration):
            upstream.close()
            return Response("Could not decrypt this file.", status=500, mimetype="text/plain")
        h["Content-Length"] = str(decrypt_len)
    elif upstream.headers.get("Content-Length"):
        h["Content-Length"] = upstream.headers["Content-Length"]

    def generate():
        try:
            if first is not None:
                yield first
            for chunk in source:
                yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), mimetype=ctype, headers=h)


@app.after_request
def secure_headers(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------- pages ----------

@app.get("/")
def home():
    return render_template("home.html", share_enabled=bool(SHARE_CHAT_ID), wall_enabled=WALL_ENABLED)


@app.get("/private")
def private_page():
    lead = "Encrypted on the server before it reaches Telegram. " if ENCRYPT_FILES else ""
    return render_template(
        "upload.html", mode="private", title="Private upload",
        subtitle=lead + "Sent to your private channel. No link is created, and nobody else can open these files.",
        endpoint="/upload", max_mb=MAX_MB, parallel=PARALLEL,
        needs_code=bool(ACCESS_CODE),
    )


@app.get("/share")
def share_page():
    if not SHARE_CHAT_ID:
        abort(404)
    return render_template(
        "upload.html", mode="share", title="Share a file",
        subtitle="Upload a file and get a link. Anyone with the link can download it.",
        endpoint="/share/upload", max_mb=SHARE_MAX_MB, parallel=PARALLEL,
        needs_code=bool(SHARE_ACCESS_CODE),
    )


# ---------- section 1: private ----------

def store_private(stream, name, size, mime, declared_mimetype):
    """Encrypt (if enabled), send to the private channel and remember it for the vault.
    Returns (row_id, error). row_id is None if Telegram sent no document back."""
    thumb = None
    if ENCRYPT_FILES:
        if mime:
            raw = make_thumb(stream)
            thumb = encrypt_bytes(raw) if raw else None
        payload = encrypt_to_tmp(stream)
        try:  # Telegram only ever sees random bytes, a random file name and no caption details
            result, err = send_document(CHAT_ID, payload, secrets.token_hex(6) + ".enc",
                                        "\U0001F512 encrypted", "application/octet-stream")
        finally:
            payload.close()
    else:
        result, err = send_document(CHAT_ID, stream, name, f"{name} ({human_size(size)})", declared_mimetype)
    if err:
        return None, err

    # Remember the upload so the vault can show it (Telegram bots can't list channel history)
    doc = result.get("document") or {}
    if not doc.get("file_id"):
        return None, None
    tg_thumb = None if ENCRYPT_FILES else (doc.get("thumbnail") or doc.get("thumb") or {}).get("file_id")
    rid = q_exec(
        "INSERT INTO private_files (message_id, file_id, thumb_id, thumb, name, size, mime, is_image, enc, created) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (result.get("message_id", 0), doc["file_id"], tg_thumb, thumb, name, size,
         mime, 1 if mime else 0, 1 if ENCRYPT_FILES else 0, time.time()),
    )
    return rid, None


@app.post("/upload")
def upload():
    if not code_ok(ACCESS_CODE, request.form.get("code")):
        return fail("Wrong access code.", 401)
    file, name, size, err = read_upload(MAX_MB)
    if err:
        return fail(*err)
    _, err = store_private(file.stream, name, size, image_mime(file.mimetype, name), file.mimetype)
    if err:
        return fail(*err)
    return jsonify(ok=True)


# ---------- section 2: share ----------

@app.post("/share/upload")
def share_upload():
    if not SHARE_CHAT_ID:
        return fail("Sharing is not set up.", 404)
    if not code_ok(SHARE_ACCESS_CODE, request.form.get("code")):
        return fail("Wrong access code.", 401)
    file, name, size, err = read_upload(SHARE_MAX_MB)
    if err:
        return fail(*err)

    token = secrets.token_urlsafe(8)
    link = f"{base_url()}/f/{token}"
    if ENCRYPT_FILES:
        payload = encrypt_to_tmp(file.stream)
        try:
            result, err = send_document(SHARE_CHAT_ID, payload, secrets.token_hex(6) + ".enc",
                                        f"\U0001F512 {link}", "application/octet-stream")
        finally:
            payload.close()
    else:
        result, err = send_document(SHARE_CHAT_ID, file.stream, name,
                                    f"{name} ({human_size(size)})\n{link}", file.mimetype)
    if err:
        return fail(*err)

    doc = result.get("document")
    if not doc:
        return fail("Telegram did not return a file ID.", 502)

    q_exec(
        "INSERT INTO shares (token, file_id, name, size, enc, created) VALUES (?,?,?,?,?,?)",
        (token, doc["file_id"], name, size, 1 if ENCRYPT_FILES else 0, time.time()),
    )
    return jsonify(ok=True, link=link)


def get_share(token):
    return q_one("SELECT * FROM shares WHERE token = ?", (token,))


@app.get("/f/<token>")
def file_page(token):
    row = get_share(token)
    if not row:
        abort(404)
    return render_template("file.html", row=row, error=None)


@app.get("/f/<token>/download")
def download(token):
    row = get_share(token)
    if not row:
        abort(404)
    upstream, err = open_telegram_file(row["file_id"])
    if err:
        return render_template("file.html", row=None, error=err[0]), err[1]
    return stream_response(
        upstream, "application/octet-stream",
        {"Content-Disposition": attachment_header(row["name"]), "Cache-Control": "private, no-store"},
        decrypt_len=row["size"] if row["enc"] else None,
    )


# ---------- vault: authenticator (TOTP, RFC 6238) ----------

def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify_totp(secret, code, last_step):
    """Returns the matched 30-second step, or None. A step can only be used once."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    now = int(time.time()) // 30
    for step in (now, now - 1, now + 1):
        if step > last_step and secrets.compare_digest(totp_code(secret, step), code):
            return step
    return None


def totp_uri(secret):
    return (f"otpauth://totp/Files%20Vault:owner?secret={secret}"
            "&issuer=Files%20Vault&algorithm=SHA1&digits=6&period=30")


# Brute-force protection: 5 wrong codes -> lock (15 min, doubling each time, max 2 h)
_lim = {"fails": 0, "locks": 0, "until": 0.0}
_lim_lock = threading.Lock()


def lock_remaining():
    with _lim_lock:
        return max(0, int(_lim["until"] - time.time()) + 1) if _lim["until"] > time.time() else 0


def note_fail():
    with _lim_lock:
        _lim["fails"] += 1
        if _lim["fails"] >= 5:
            _lim["locks"] += 1
            _lim["until"] = time.time() + min(900 * 2 ** (_lim["locks"] - 1), 7200)
            _lim["fails"] = 0


def note_ok():
    with _lim_lock:
        _lim.update(fails=0, locks=0, until=0.0)


def locked_response(wait):
    mins = max(1, round(wait / 60))
    return fail(f"Too many wrong codes. Try again in about {mins} min.", 429, retry_after=wait)


def json_body():
    # Requiring JSON blocks cross-site form posts (CSRF) because browsers can't send it cross-origin
    if not request.is_json:
        abort(415)
    return request.get_json(silent=True) or {}


def logged_in():
    gen = get_setting("auth_gen")
    return bool(gen) and session.get("vault_gen") == gen


def start_session():
    session.clear()
    session.permanent = True
    session["vault_gen"] = get_setting("auth_gen")


OPEN_COOKIE = "vault_open"
_COOKIE_OPTS = dict(httponly=True, samesite="Strict", secure=BASE_URL.startswith("https://"))
# The vault lives at a random address (/v/<token>) created by the hidden 10-tap entrance. It stays valid
# while you use it and dies after VAULT_IDLE_MINUTES (+1 min grace) without activity, or when you leave/sign out.
LINK_TTL = VAULT_IDLE_MINUTES * 60 + 60 if VAULT_IDLE_MINUTES > 0 else 12 * 3600


def new_link():
    tok = secrets.token_urlsafe(24)
    set_setting("vault_link", f"{tok}|{time.time()}")
    return tok


def link_valid(tok):
    """True if tok is the current vault address token and has not expired. Counts as activity."""
    cur, _, ts = (get_setting("vault_link") or "").partition("|")
    try:
        last = float(ts)
    except ValueError:
        return False
    if not tok or not cur or not secrets.compare_digest(cur.encode(), str(tok).encode()):
        return False
    now = time.time()
    if now - last > LINK_TTL:
        set_setting("vault_link", None)
        return False
    if now - last > 10:
        set_setting("vault_link", f"{cur}|{now}")
    return True


def vault_is_open():
    return link_valid(request.cookies.get(OPEN_COOKIE, ""))


def close_vault(resp):
    set_setting("vault_link", None)
    resp.delete_cookie(OPEN_COOKIE)
    return resp


def vault_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not logged_in() or not vault_is_open():
            return fail("Please sign in again.", 401)
        return fn(*a, **kw)
    return wrapper


def link_required(fn):
    """Setup / sign-in steps only answer to a browser that came in through the live vault address."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if not vault_is_open():
            abort(404)
        return fn(*a, **kw)
    return wrapper


def vault_state():
    if logged_in():
        return "manager"
    if not get_setting("totp_secret"):
        if not VAULT_SETUP_KEY:
            return "disabled"
        return "setup_qr" if session.get("setup_ok") and get_setting("pending_totp") else "setup_key"
    return "login"


@app.post("/vault/enter")
def vault_enter():
    """Hidden entrance (home page, 10 taps): makes a brand-new random vault address and returns it.
    It is a background request, so it never appears in the browser history."""
    json_body()
    return jsonify(ok=True, url=f"/v/{new_link()}")


@app.get("/v/<token>")
def vault(token):
    if not link_valid(token):
        abort(404)  # expired / wrong / old history entry: looks like any missing page
    state = vault_state()
    if state == "manager":
        resp = app.make_response(render_template("vault.html", idle_minutes=VAULT_IDLE_MINUTES))
    else:
        ctx = {}
        if state == "setup_qr":
            secret = get_setting("pending_totp")
            uri = totp_uri(secret)
            qr = ""
            if segno:
                try:
                    qr = segno.make(uri, error="m").svg_inline(scale=6, border=2, dark="#000", light="#fff")
                except Exception:
                    qr = ""
            ctx = dict(uri=uri, qr=qr, secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
        resp = app.make_response(render_template("vault_login.html", state=state, **ctx))
    resp.set_cookie(OPEN_COOKIE, token, **_COOKIE_OPTS)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/vault/ping")
@vault_required
def vault_ping():
    return jsonify(ok=True)  # the activity itself (any authenticated request) keeps the address alive


@app.post("/vault/setup/key")
@link_required
def vault_setup_key():
    body = json_body()
    if get_setting("totp_secret") or not VAULT_SETUP_KEY:
        return fail("Setup is not available.", 403)
    wait = lock_remaining()
    if wait:
        return locked_response(wait)
    if not secrets.compare_digest(VAULT_SETUP_KEY.encode(), str(body.get("key", "")).strip().encode()):
        note_fail()
        return fail("Wrong setup key.", 401)
    note_ok()
    set_setting("pending_totp", new_totp_secret())
    session["setup_ok"] = True
    return jsonify(ok=True)


@app.post("/vault/setup/confirm")
@link_required
def vault_setup_confirm():
    body = json_body()
    if get_setting("totp_secret") or not session.get("setup_ok"):
        return fail("Setup is not available.", 403)
    secret = get_setting("pending_totp")
    if not secret:
        return fail("Start over and enter the setup key again.", 400)
    wait = lock_remaining()
    if wait:
        return locked_response(wait)
    step = verify_totp(secret, body.get("code"), -1)
    if step is None:
        note_fail()
        return fail("That code didn't match. Check your phone's clock and try again.", 401)
    note_ok()
    set_setting("totp_secret", secret)
    set_setting("pending_totp", None)
    set_setting("last_step", str(step))
    set_setting("auth_gen", secrets.token_hex(16))
    start_session()
    return jsonify(ok=True)


@app.post("/vault/login")
@link_required
def vault_login():
    body = json_body()
    secret = get_setting("totp_secret")
    if not secret:
        return fail("Setup is not finished.", 403)
    wait = lock_remaining()
    if wait:
        return locked_response(wait)
    step = verify_totp(secret, body.get("code"), int(get_setting("last_step", "-1")))
    if step is None:
        note_fail()
        wait = lock_remaining()
        return locked_response(wait) if wait else fail("Wrong code.", 401)
    note_ok()
    set_setting("last_step", str(step))
    start_session()
    return jsonify(ok=True)


@app.post("/vault/close")
def vault_close():
    """Close the vault (idle / panic) but keep the login session."""
    json_body()
    return close_vault(jsonify(ok=True))


@app.post("/vault/logout")
def vault_logout():
    json_body()
    session.clear()
    return close_vault(jsonify(ok=True))


# ---------- upload control (device registration + vault management) ----------

@app.post("/v1/device/register")
def upload_device_register():
    body = json_body()
    device_id = str(body.get("device_id", "")).strip()[:128]
    if not device_id:
        return fail("Invalid device.", 400)
    now = time.time()
    row = q_one("SELECT * FROM upload_devices WHERE device_id = ?", (device_id,))
    if row:
        supplied = str(body.get("token") or "")
        if supplied and not secrets.compare_digest(supplied, row["token"]):
            return fail("Invalid device token.", 403)
        token = row["token"]
        q_exec(
            "UPDATE upload_devices SET model = ?, app_version = ?, sdk = ?, last_seen = ? WHERE device_id = ?",
            (str(body.get("model", ""))[:120], str(body.get("app_version", ""))[:40],
             int(body.get("sdk", 0) or 0), now, device_id),
        )
        config = upload_config(row)
    else:
        token = secrets.token_urlsafe(32)
        config = dict(UPLOAD_DEFAULT_CONFIG)
        q_exec(
            "INSERT INTO upload_devices "
            "(device_id, token, model, app_version, sdk, config, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, token, str(body.get("model", ""))[:120], str(body.get("app_version", ""))[:40],
             int(body.get("sdk", 0) or 0), json.dumps(config, separators=(",", ":")), now, now),
        )
    return jsonify(token=token, config=config)


@app.get("/vault/api/upload-devices")
@vault_required
def upload_devices():
    rows = q_all(
        "SELECT device_id, model, app_version, sdk, config, first_seen, last_seen "
        "FROM upload_devices ORDER BY last_seen DESC, device_id"
    )
    items = []
    for row in rows:
        items.append({
            "device_id": row["device_id"],
            "model": row["model"],
            "app_version": row["app_version"],
            "sdk": row["sdk"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "config": upload_config(row),
        })
    return jsonify(ok=True, items=items)


@app.post("/vault/api/upload-devices/config")
@vault_required
def upload_device_config():
    body = json_body()
    device_id = str(body.get("device_id", "")).strip()
    row = q_one("SELECT * FROM upload_devices WHERE device_id = ?", (device_id,))
    if not row:
        return fail("Device not found.", 404)
    try:
        config = clean_upload_config(body.get("config"))
    except (KeyError, TypeError, ValueError):
        return fail("Invalid upload configuration.", 400)
    q_exec(
        "UPDATE upload_devices SET config = ? WHERE device_id = ?",
        (json.dumps(config, separators=(",", ":")), device_id),
    )
    return jsonify(ok=True, device_id=device_id, config=config)


# ---------- vault: file manager ----------

def get_file(fid):
    return q_one("SELECT * FROM private_files WHERE id = ?", (fid,))


def forget_file(fid):
    """Remove a file from the database, including its folder links."""
    with conn() as c:
        c.execute("DELETE FROM folder_files WHERE file_id = ?", (fid,))
        c.execute("DELETE FROM private_files WHERE id = ?", (fid,))


@app.get("/vault/api/files")
@vault_required
def vault_files():
    q = request.args.get("q", "").strip()
    offset = max(0, request.args.get("offset", 0, type=int) or 0)
    sort = request.args.get("sort", "date")
    desc = request.args.get("dir", "desc") != "asc"
    seed = request.args.get("seed", 1, type=int) or 1
    limit = max(1, min(request.args.get("limit", 60, type=int), 200))

    folder = request.args.get("folder", type=int)

    # Images over Telegram's 20 MB bot-download limit are never shown in the vault
    where, args = "is_image = 1 AND size <= ?", [VAULT_MAX_BYTES]
    mode = request.args.get("mode", "")
    if mode == "ignored":  # the Ignored section: every ignored image, wherever it lives
        where += " AND ignored = 1"
    elif not folder and mode != "fav":  # ignored images are hidden from Photos / Memories / search, but stay visible in folders and Favourites
        where += " AND ignored = 0"
    if mode == "fav":
        where += " AND fav = 1"
    elif mode == "memories":  # "On this day": same month+day, earlier years (client sends its own date + UTC offset)
        md = request.args.get("md", "")
        year = request.args.get("year", type=int) or 0
        tz = max(-50400, min(request.args.get("tz", 0, type=int) or 0, 50400))
        if not re.fullmatch(r"\d\d-\d\d", md):
            return fail("Bad date.", 400)
        where += (" AND strftime('%m-%d', created + ?, 'unixepoch') = ?"
                  " AND CAST(strftime('%Y', created + ?, 'unixepoch') AS INTEGER) < ?")
        args += [tz, md, tz, year]
    if folder:
        where += " AND id IN (SELECT file_id FROM folder_files WHERE folder_id = ?)"
        args.append(folder)
    if q:  # search looks at the caption and the file name
        esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        where += " AND (caption LIKE ? ESCAPE '!' OR name LIKE ? ESCAPE '!')"
        args += [f"%{esc}%", f"%{esc}%"]
    total = q_one(f"SELECT COUNT(*) AS n FROM private_files WHERE {where}", args)["n"]
    d = "DESC" if desc else "ASC"
    cols = "id, name, size, created, caption, fav, ignored"
    if sort == "random":
        # Same seed = same shuffle, so scrolling through pages never repeats or skips a photo
        all_ids = [r["id"] for r in q_all(f"SELECT id FROM private_files WHERE {where} ORDER BY id", args)]
        random.Random(seed).shuffle(all_ids)
        page_ids = all_ids[offset:offset + limit + 1]
        by_id = {r["id"]: r for r in q_all(
            f"SELECT {cols} FROM private_files WHERE id IN ({','.join('?' * len(page_ids))})", page_ids)} if page_ids else {}
        rows = [by_id[i] for i in page_ids if i in by_id]
    else:
        order = f"name COLLATE NOCASE {d}, id {d}" if sort == "name" else f"size {d}, id {d}" if sort == "size" else f"id {d}"
        rows = q_all(
            f"SELECT {cols} FROM private_files WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [limit + 1, offset],
        )
    return jsonify(ok=True, total=total, has_more=len(rows) > limit, items=rows[:limit])


@app.get("/vault/img/<int:fid>/<kind>")
@vault_required
def vault_img(fid, kind):
    row = get_file(fid)
    if kind not in ("thumb", "full") or not row or not row["is_image"] or row["size"] > VAULT_MAX_BYTES:
        abort(404)
    safe = {"Content-Security-Policy": "default-src 'none'; sandbox"}

    if kind == "thumb":
        if row["thumb"]:  # our own encrypted preview, served straight from the database
            try:
                data = decrypt_bytes(row["thumb"])
            except DecryptError:
                return Response("Could not decrypt this preview.", status=500, mimetype="text/plain")
            return Response(data, mimetype="image/jpeg",
                            headers={**safe, "Cache-Control": "private, max-age=86400"})
        if row["thumb_id"]:  # older uploads: Telegram's own preview
            upstream, err = open_telegram_file(row["thumb_id"])
            if err:
                return Response(err[0], status=err[1], mimetype="text/plain")
            return stream_response(upstream, "image/jpeg", {**safe, "Cache-Control": "private, max-age=86400"})

    upstream, err = open_telegram_file(row["file_id"])
    if err:
        return Response(err[0], status=err[1], mimetype="text/plain")
    return stream_response(
        upstream, row["mime"], {**safe, "Cache-Control": "private, max-age=600"},
        decrypt_len=row["size"] if row["enc"] else None,
    )


@app.get("/vault/dl/<int:fid>")
@vault_required
def vault_download(fid):
    row = get_file(fid)
    if not row:
        abort(404)
    upstream, err = open_telegram_file(row["file_id"])
    if err:
        return Response(err[0], status=err[1], mimetype="text/plain")
    return stream_response(
        upstream, "application/octet-stream",
        {"Content-Disposition": attachment_header(row["name"]), "Cache-Control": "private, no-store"},
        decrypt_len=row["size"] if row["enc"] else None,
    )


EDIT_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


def edited_name(source_name, ext):
    """photo.jpg -> photo-edited.jpg, then photo-edited-2.jpg, ... so copies never share a name."""
    stem = re.sub(r"-edited(-\d+)?$", "", os.path.splitext(source_name)[0])[:120] or "photo"
    for n in range(1, 100):
        cand = f"{stem}-edited{'' if n == 1 else f'-{n}'}{ext}"
        if not q_one("SELECT 1 AS x FROM private_files WHERE name = ?", (cand,)):
            return cand
    return f"{stem}-edited-{int(time.time())}{ext}"


@app.post("/vault/api/edit/save")
@vault_required
def vault_edit_save():
    """The photo editor uploads its result here. It becomes a NEW photo (the original is left alone) that is
    standalone apart from being filed in the "Edits" folder."""
    if request.headers.get("X-Vault") != "1":  # a custom header can't be sent cross-site, like the JSON requirement elsewhere
        abort(415)
    src = get_file(request.form.get("source_id", type=int) or 0)
    if not src or not src["is_image"]:
        return fail("The original photo was not found.", 404)
    file, _, size, err = read_upload(MAX_MB)
    if err:
        return fail(*err)

    # Make sure it really is an ordinary image before anything is sent to Telegram
    fmt = None
    if Image is not None:
        try:
            Image.MAX_IMAGE_PIXELS = 60_000_000
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                probe = Image.open(file.stream)
                fmt = probe.format
                probe.verify()
        except Exception:
            return fail("That doesn't look like a valid image.", 400)
        finally:
            file.stream.seek(0)
    if fmt not in EDIT_FORMATS:
        return fail("Unsupported image format.", 400)
    mime, ext = EDIT_FORMATS[fmt]

    name = edited_name(src["name"], ext)
    rid, err = store_private(file.stream, name, size, mime, mime)
    if err:
        return fail(*err)
    if not rid:
        return fail("Telegram did not return a file ID.", 502)

    # The edit is a standalone photo: no caption, favourite, ignore flag or folders are copied from the original.
    # The only folder it joins is the special "Edits" folder.
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO folders (name, created) VALUES (?, ?)", (EDITS_FOLDER, time.time()))
        row = c.execute("SELECT id FROM folders WHERE name = ?", (EDITS_FOLDER,)).fetchone()
        c.execute("INSERT OR IGNORE INTO folder_files (folder_id, file_id, added) VALUES (?, ?, ?)",
                  (row[0], rid, time.time()))
    return jsonify(ok=True, id=rid, name=name)


@app.post("/vault/api/delete")
@vault_required
def vault_delete():
    ids = [i for i in (json_body().get("ids") or []) if isinstance(i, int)][:100]
    deleted, failed = [], []
    for fid in ids:
        row = get_file(fid)
        if not row:
            forget_file(fid)
            deleted.append(fid)
            continue
        try:
            data = session_http.post(
                f"{API}/deleteMessage",
                data={"chat_id": CHAT_ID, "message_id": row["message_id"]}, timeout=30,
            ).json()
        except (requests.RequestException, ValueError):
            failed.append({"id": fid, "error": "Telegram is not responding."})
            continue
        gone = "message to delete not found" in str(data.get("description", "")).lower()
        if data.get("ok") or gone:
            forget_file(fid)
            deleted.append(fid)
        else:
            failed.append({"id": fid, "error": data.get("description", "Telegram refused.")})
    return jsonify(ok=True, deleted=deleted, failed=failed)


@app.post("/vault/api/fav")
@vault_required
def vault_fav():
    body = json_body()
    val = 1 if body.get("fav") else 0
    ids = int_ids(body)
    with conn() as c:
        for fid in ids:
            c.execute("UPDATE private_files SET fav = ? WHERE id = ? AND is_image = 1", (val, fid))
    return jsonify(ok=True, ids=ids, fav=val)


@app.post("/vault/api/ignore")
@vault_required
def vault_ignore():
    """Hide images from the main photo views (they stay in folders) or bring them back."""
    body = json_body()
    val = 1 if body.get("ignored") else 0
    ids = int_ids(body)
    with conn() as c:
        for fid in ids:
            c.execute("UPDATE private_files SET ignored = ? WHERE id = ? AND is_image = 1", (val, fid))
    return jsonify(ok=True, ids=ids, ignored=val)


@app.get("/vault/api/info/<int:fid>")
@vault_required
def vault_info(fid):
    row = get_file(fid)
    if not row:
        return fail("Not found.", 404)
    folders = q_all(
        "SELECT f.id, f.name FROM folders f JOIN folder_files ff ON ff.folder_id = f.id "
        "WHERE ff.file_id = ? ORDER BY f.name", (fid,))
    stats = q_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes, COALESCE(SUM(fav), 0) AS favs, "
        "COALESCE(SUM(ignored), 0) AS ignored "
        "FROM private_files WHERE is_image = 1 AND size <= ?", (VAULT_MAX_BYTES,))
    return jsonify(ok=True, id=fid, name=row["name"], size=row["size"], mime=row["mime"],
                   created=row["created"], caption=row["caption"], fav=row["fav"], ignored=row["ignored"],
                   encrypted=bool(row["enc"]), folders=folders,
                   vault={"photos": stats["n"], "bytes": stats["bytes"], "favs": stats["favs"],
                          "ignored": stats["ignored"]})


# ---------- vault: captions + folders ----------

MAX_CAPTION = 500
MAX_FOLDER_NAME = 60
EDITS_FOLDER = "Edits"  # holds every edited photo; always listed first, cannot be renamed


def int_ids(body, key="ids", limit=500):
    return [i for i in (body.get(key) or []) if isinstance(i, int) and not isinstance(i, bool)][:limit]


def clean_folder_name(raw):
    name = " ".join(str(raw or "").split())  # trim and collapse whitespace
    return name[:MAX_FOLDER_NAME]


def folder_or_none(fid):
    if not isinstance(fid, int) or isinstance(fid, bool):
        return None
    return q_one("SELECT id, name FROM folders WHERE id = ?", (fid,))


@app.post("/vault/api/caption")
@vault_required
def vault_caption():
    body = json_body()
    fid = body.get("id")
    if not isinstance(fid, int) or isinstance(fid, bool):
        return fail("Invalid image.", 400)
    row = get_file(fid)
    if not row or not row["is_image"]:
        return fail("Image not found.", 404)
    caption = str(body.get("caption") or "").replace("\r\n", "\n").strip()[:MAX_CAPTION]
    q_exec("UPDATE private_files SET caption = ? WHERE id = ?", (caption, fid))
    return jsonify(ok=True, caption=caption)


@app.get("/vault/api/folders")
@vault_required
def folders_list():
    rows = q_all(
        "SELECT f.id, f.name, f.created, "
        "(SELECT COUNT(*) FROM folder_files x JOIN private_files p ON p.id = x.file_id "
        " WHERE x.folder_id = f.id AND p.size <= ?) AS n, "
        "(SELECT MAX(x.file_id) FROM folder_files x JOIN private_files p ON p.id = x.file_id "
        " WHERE x.folder_id = f.id AND p.size <= ?) AS cover "
        "FROM folders f ORDER BY (f.name = ? COLLATE NOCASE) DESC, f.name COLLATE NOCASE",
        (VAULT_MAX_BYTES, VAULT_MAX_BYTES, EDITS_FOLDER),
    )
    return jsonify(ok=True, items=rows)


@app.post("/vault/api/folders")
@vault_required
def folders_create():
    name = clean_folder_name(json_body().get("name"))
    if not name:
        return fail("Give the folder a name.", 400)
    try:
        fid = q_exec("INSERT INTO folders (name, created) VALUES (?, ?)", (name, time.time()))
    except sqlite3.IntegrityError:
        return fail("A folder with that name already exists.", 409)
    return jsonify(ok=True, folder={"id": fid, "name": name, "n": 0, "cover": None})


@app.post("/vault/api/folders/rename")
@vault_required
def folders_rename():
    body = json_body()
    folder = folder_or_none(body.get("id"))
    if not folder:
        return fail("Folder not found.", 404)
    if folder["name"].lower() == EDITS_FOLDER.lower():
        return fail("The Edits folder can't be renamed.", 400)
    name = clean_folder_name(body.get("name"))
    if not name:
        return fail("Give the folder a name.", 400)
    try:
        q_exec("UPDATE folders SET name = ? WHERE id = ?", (name, folder["id"]))
    except sqlite3.IntegrityError:
        return fail("A folder with that name already exists.", 409)
    return jsonify(ok=True, name=name)


@app.post("/vault/api/folders/delete")
@vault_required
def folders_delete():
    folder = folder_or_none(json_body().get("id"))
    if not folder:
        return fail("Folder not found.", 404)
    with conn() as c:  # only the folder goes away; the photos stay in the vault
        c.execute("DELETE FROM folder_files WHERE folder_id = ?", (folder["id"],))
        c.execute("DELETE FROM folders WHERE id = ?", (folder["id"],))
    return jsonify(ok=True)


@app.post("/vault/api/folders/add")
@vault_required
def folders_add():
    body = json_body()
    folder = folder_or_none(body.get("folder_id"))
    if not folder:
        return fail("Folder not found.", 404)
    added, now = 0, time.time()
    with conn() as c:
        for fid in int_ids(body):
            cur = c.execute(
                "INSERT OR IGNORE INTO folder_files (folder_id, file_id, added) "
                "SELECT ?, id, ? FROM private_files WHERE id = ? AND is_image = 1 AND size <= ?",
                (folder["id"], now, fid, VAULT_MAX_BYTES),
            )
            added += cur.rowcount
    return jsonify(ok=True, added=added)


@app.post("/vault/api/folders/remove")
@vault_required
def folders_remove():
    body = json_body()
    folder = folder_or_none(body.get("folder_id"))
    if not folder:
        return fail("Folder not found.", 404)
    removed = []
    with conn() as c:
        for fid in int_ids(body):
            c.execute("DELETE FROM folder_files WHERE folder_id = ? AND file_id = ?", (folder["id"], fid))
            removed.append(fid)
    return jsonify(ok=True, removed=removed)


# ---------- wallpapers: Telegram-backed wallpaper hosting + public JSON for the Android app ----------
# The panel (/wallpapers) is protected by its OWN authenticator setup. The app only uses the public
# endpoints: /frames.json (the list) and /w/<token>.<ext> (images, streamed from Telegram, long-cached).

WALL_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


class Limiter:
    """5 wrong codes -> lock (15 min, doubling each time, max 2 h)."""
    def __init__(self):
        self.fails = self.locks = 0
        self.until = 0.0
        self.mu = threading.Lock()

    def remaining(self):
        with self.mu:
            return int(self.until - time.time()) + 1 if self.until > time.time() else 0

    def fail(self):
        with self.mu:
            self.fails += 1
            if self.fails >= 5:
                self.locks += 1
                self.until = time.time() + min(900 * 2 ** (self.locks - 1), 7200)
                self.fails = 0

    def ok(self):
        with self.mu:
            self.fails = self.locks = 0
            self.until = 0.0


wall_lim = Limiter()


def wall_logged_in():
    gen = get_setting("wall_auth_gen")
    return bool(gen) and session.get("wall_gen") == gen


def wall_on(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not WALL_ENABLED:
            abort(404)
        return fn(*a, **kw)
    return wrapper


def wall_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not WALL_ENABLED:
            abort(404)
        if not wall_logged_in():
            return fail("Please sign in again.", 401)
        return fn(*a, **kw)
    return wrapper


def wall_state():
    if wall_logged_in():
        return "manager"
    if not get_setting("wall_totp_secret"):
        if not WALL_SETUP_KEY:
            return "disabled"
        return "setup_qr" if session.get("wall_setup_ok") and get_setting("wall_pending_totp") else "setup_key"
    return "login"


def _locked():
    wait = wall_lim.remaining()
    return locked_response(wait) if wait else None


@app.get("/wallpapers")
@wall_on
def wall_page():
    state = wall_state()
    if state == "manager":
        resp = app.make_response(render_template("wallpapers.html"))
    else:
        ctx = {}
        if state == "setup_qr":
            secret = get_setting("wall_pending_totp")
            uri = (f"otpauth://totp/Wallpapers:owner?secret={secret}"
                   "&issuer=Wallpapers&algorithm=SHA1&digits=6&period=30")
            qr = ""
            if segno:
                try:
                    qr = segno.make(uri, error="m").svg_inline(scale=6, border=2, dark="#000", light="#fff")
                except Exception:
                    qr = ""
            ctx = dict(uri=uri, qr=qr, secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
        resp = app.make_response(render_template("wall_login.html", state=state, **ctx))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/wallpapers/setup/key")
@wall_on
def wall_setup_key():
    body = json_body()
    if get_setting("wall_totp_secret") or not WALL_SETUP_KEY:
        return fail("Setup is not available.", 403)
    if (r := _locked()):
        return r
    if not secrets.compare_digest(WALL_SETUP_KEY.encode(), str(body.get("key", "")).strip().encode()):
        wall_lim.fail()
        return fail("Wrong setup key.", 401)
    wall_lim.ok()
    set_setting("wall_pending_totp", new_totp_secret())
    session["wall_setup_ok"] = True
    return jsonify(ok=True)


@app.post("/wallpapers/setup/confirm")
@wall_on
def wall_setup_confirm():
    body = json_body()
    if get_setting("wall_totp_secret") or not session.get("wall_setup_ok"):
        return fail("Setup is not available.", 403)
    secret = get_setting("wall_pending_totp")
    if not secret:
        return fail("Start over and enter the setup key again.", 400)
    if (r := _locked()):
        return r
    step = verify_totp(secret, body.get("code"), -1)
    if step is None:
        wall_lim.fail()
        return fail("That code didn't match. Check your phone's clock and try again.", 401)
    wall_lim.ok()
    set_setting("wall_totp_secret", secret)
    set_setting("wall_pending_totp", None)
    set_setting("wall_last_step", str(step))
    gen = secrets.token_hex(16)
    set_setting("wall_auth_gen", gen)
    session.permanent = True
    session["wall_gen"] = gen
    session.pop("wall_setup_ok", None)
    return jsonify(ok=True)


@app.post("/wallpapers/login")
@wall_on
def wall_login():
    body = json_body()
    secret = get_setting("wall_totp_secret")
    if not secret:
        return fail("Setup is not finished.", 403)
    if (r := _locked()):
        return r
    step = verify_totp(secret, body.get("code"), int(get_setting("wall_last_step", "-1")))
    if step is None:
        wall_lim.fail()
        return _locked() or fail("Wrong code.", 401)
    wall_lim.ok()
    set_setting("wall_last_step", str(step))
    session.permanent = True
    session["wall_gen"] = get_setting("wall_auth_gen")
    return jsonify(ok=True)


@app.post("/wallpapers/logout")
@wall_on
def wall_logout():
    json_body()
    session.pop("wall_gen", None)
    return jsonify(ok=True)


# ----- data helpers -----

def clean_text(v, n):
    return " ".join(str(v or "").split())[:n]


def clean_collections(raw, featured):
    """Comma list -> unique tags. The Featured switch is the source of truth for the 'featured' tag."""
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    out, seen = [], set()
    for p in items:
        p = clean_text(p, 40)
        if p and p.lower() not in seen and p.lower() != "featured":
            seen.add(p.lower())
            out.append(p)
    return (["featured"] if featured else []) + out


def wall_urls(r):
    b = base_url()
    return f"{b}/w/{r['token']}{r['ext']}", f"{b}/w/{r['token']}_t.jpg"


WALL_COLS = ("id, token, name, author, copyright, collections, featured, downloadable, "
             "width, height, size, mime, ext, created")


def build_wall_json():
    """The exact list the Android app reads (same shape as frames.json)."""
    out = []
    for r in q_all(f"SELECT {WALL_COLS} FROM wallpapers ORDER BY id DESC"):
        url, thumb = wall_urls(r)
        item = {"name": r["name"]}
        if r["author"]:
            item["author"] = r["author"]
        item["url"] = url
        item["thumbnail"] = thumb
        item["dimensions"] = f"{r['width']}x{r['height']}"
        if r["copyright"]:
            item["copyright"] = r["copyright"]
        if r["collections"]:
            item["collections"] = r["collections"]
        if r["featured"]:
            item["featured"] = True
        if not r["downloadable"]:
            item["downloadable"] = False
        out.append(item)
    return out


def write_wall_json():
    """Keep a copy of the JSON on disk too (backup / for hosting elsewhere)."""
    try:
        tmp = WALL_JSON_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(build_wall_json(), f, indent=2, ensure_ascii=False)
        os.replace(tmp, WALL_JSON_PATH)
    except OSError:
        pass


def wall_row(wid):
    return q_one(f"SELECT {WALL_COLS}, message_id FROM wallpapers WHERE id = ?", (wid,))


def wall_public_item(r):
    url, thumb = wall_urls(r)
    return dict(id=r["id"], name=r["name"], author=r["author"], copyright=r["copyright"],
                collections=r["collections"], featured=bool(r["featured"]),
                downloadable=bool(r["downloadable"]), dimensions=f"{r['width']}x{r['height']}",
                size=r["size"], url=url, thumbnail=thumb)


# ----- public endpoints (used by the Android app) -----

@app.get("/frames.json")
@wall_on
def wall_json():
    return Response(json.dumps(build_wall_json(), indent=2, ensure_ascii=False),
                    mimetype="application/json",
                    headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


@app.get("/w/<name>")
@wall_on
def wall_public(name):
    thumb = name.endswith("_t.jpg")
    if thumb:
        token, ext = name[:-6], ".jpg"
    else:
        token, dot, ext = name.rpartition(".")
        ext = dot + ext
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        abort(404)
    row = q_one("SELECT * FROM wallpapers WHERE token = ?", (token,))
    if not row or (not thumb and ext != row["ext"]):
        abort(404)
    cache = {"Cache-Control": "public, max-age=2592000, immutable", "Access-Control-Allow-Origin": "*"}
    if thumb and row["thumb"]:
        return Response(row["thumb"], mimetype="image/jpeg", headers=cache)
    upstream, err = open_telegram_file(row["file_id"])
    if err:
        return Response(err[0], status=err[1], mimetype="text/plain")
    return stream_response(upstream, row["mime"], cache)


# ----- panel API -----

@app.get("/wallpapers/api/list")
@wall_required
def wall_list():
    rows = q_all(f"SELECT {WALL_COLS} FROM wallpapers ORDER BY id DESC")
    return jsonify(ok=True, items=[wall_public_item(r) for r in rows], json_url=f"{base_url()}/frames.json")


@app.post("/wallpapers/api/upload")
@wall_required
def wall_upload():
    if request.headers.get("X-Vault") != "1":
        abort(415)
    file, fname, size, err = read_upload(WALL_MAX_MB)
    if err:
        return fail(*err)
    if Image is None:
        return fail("Pillow is not installed on the server.", 500)
    try:
        Image.MAX_IMAGE_PIXELS = 100_000_000
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probe = Image.open(file.stream)
            fmt, (w, h) = probe.format, probe.size
            probe.verify()
    except Exception:
        return fail("That doesn't look like a valid image.", 400)
    finally:
        file.stream.seek(0)
    if fmt not in WALL_FORMATS:
        return fail("Only JPG, PNG and WEBP are supported.", 400)
    mime, ext = WALL_FORMATS[fmt]

    name = clean_text(request.form.get("name"), 80) or clean_text(os.path.splitext(fname)[0], 80) or "Wallpaper"
    author = clean_text(request.form.get("author"), 60)
    copy = clean_text(request.form.get("copyright"), 120)
    featured = request.form.get("featured") == "1"
    downloadable = request.form.get("downloadable", "1") != "0"
    collections = ",".join(clean_collections(request.form.get("collections"), featured))

    thumb = make_thumb(file.stream, WALL_THUMB_PX)
    token = secrets.token_hex(8)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:50] or "wallpaper"
    caption = (f"{name}" + (f" - {author}" if author else "") + f"\n{w}x{h} | {human_size(size)}")[:1000]
    result, err = send_document(WALL_CHAT_ID, file.stream, slug + ext, caption, mime)
    if err:
        return fail(*err)
    doc = (result or {}).get("document") or {}
    if not doc.get("file_id"):
        return fail("Telegram did not return a file ID.", 502)

    wid = q_exec(
        "INSERT INTO wallpapers (token, message_id, file_id, name, author, copyright, collections, featured, "
        "downloadable, width, height, size, mime, ext, thumb, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (token, result.get("message_id", 0), doc["file_id"], name, author, copy, collections,
         1 if featured else 0, 1 if downloadable else 0, w, h, size, mime, ext, thumb, time.time()),
    )
    write_wall_json()
    return jsonify(ok=True, item=wall_public_item(wall_row(wid)))


@app.post("/wallpapers/api/update")
@wall_required
def wall_update():
    body = json_body()
    wid = body.get("id")
    row = wall_row(wid) if isinstance(wid, int) and not isinstance(wid, bool) else None
    if not row:
        return fail("Wallpaper not found.", 404)
    name = clean_text(body.get("name"), 80)
    if not name:
        return fail("Give it a name.", 400)
    featured = bool(body.get("featured"))
    q_exec(
        "UPDATE wallpapers SET name=?, author=?, copyright=?, collections=?, featured=?, downloadable=? WHERE id=?",
        (name, clean_text(body.get("author"), 60), clean_text(body.get("copyright"), 120),
         ",".join(clean_collections(body.get("collections"), featured)), 1 if featured else 0,
         1 if body.get("downloadable", True) else 0, wid),
    )
    write_wall_json()
    return jsonify(ok=True, item=wall_public_item(wall_row(wid)))


@app.post("/wallpapers/api/delete")
@wall_required
def wall_delete():
    ids = [i for i in (json_body().get("ids") or []) if isinstance(i, int) and not isinstance(i, bool)][:50]
    deleted, failed = [], []
    for wid in ids:
        row = wall_row(wid)
        if row:
            try:
                data = session_http.post(f"{API}/deleteMessage",
                                         data={"chat_id": WALL_CHAT_ID, "message_id": row["message_id"]},
                                         timeout=30).json()
            except (requests.RequestException, ValueError):
                failed.append({"id": wid, "error": "Telegram is not responding."})
                continue
            gone = "message to delete not found" in str(data.get("description", "")).lower()
            if not (data.get("ok") or gone):
                failed.append({"id": wid, "error": data.get("description", "Telegram refused.")})
                continue
        q_exec("DELETE FROM wallpapers WHERE id = ?", (wid,))
        deleted.append(wid)
    write_wall_json()
    return jsonify(ok=True, deleted=deleted, failed=failed)


# ---------- errors ----------

@app.errorhandler(404)
def not_found(_):
    return render_template("file.html", row=None, error="This link doesn't exist."), 404


@app.errorhandler(413)
def too_large(_):
    return jsonify(ok=False, error="File is too large."), 413


@app.errorhandler(415)
def bad_type(_):
    return jsonify(ok=False, error="Unsupported request."), 415


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "45270")), debug=False, threaded=True)
