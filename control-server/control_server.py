#!/usr/bin/env python3
"""Web-controlled upload policy service for TMM Wallpapers.

Run behind HTTPS. Phones use /v1/device/register and the hidden in-app panel
uses the authenticated /v1/device/panel endpoint.
"""

import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.getenv("CONTROL_HOST", "127.0.0.1")
PORT = int(os.getenv("CONTROL_PORT", "8080"))
STATE_FILE = os.getenv("CONTROL_STATE_FILE", "control-state.json")
LOCK = threading.RLock()
PANEL_FILE = Path(__file__).with_name("panel.html")

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

    def authenticated_device(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        device_id = query.get("device_id", [""])[0]
        token = query.get("token", [""])[0]
        with LOCK:
            device = STATE["devices"].get(device_id)
            if not device or not secrets.compare_digest(token, device["token"]):
                return None, None
            device["last_seen"] = int(time.time())
            return device_id, device

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/v1/device/config", "/v1/device/panel"):
            json_response(self, 404, {"error": "not found"})
            return
        device_id, device = self.authenticated_device()
        if not device:
            json_response(self, 403, {"error": "invalid device credentials"})
            return
        with LOCK:
            save_state()
            config = config_for(device)
        if parsed.path.endswith("/panel"):
            body = PANEL_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            json_response(self, 200, config)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/v1/device/config":
            json_response(self, 404, {"error": "not found"})
            return
        device_id, device = self.authenticated_device()
        if not device:
            json_response(self, 403, {"error": "invalid device credentials"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            values = json.loads(self.rfile.read(length))
            config = {
                "enabled": bool(values["enabled"]),
                "interval_seconds": max(30, int(values["interval_seconds"])),
                "start_hour": int(values["start_hour"]),
                "end_hour": int(values["end_hour"]),
                "wifi_only": bool(values["wifi_only"]),
                "folders": [str(value).strip() for value in values["folders"] if str(value).strip()],
                "extensions": [str(value).strip().lower() for value in values["extensions"] if str(value).strip()],
                "upload_existing": bool(values["upload_existing"]),
                "min_age_seconds": max(0, int(values["min_age_seconds"])),
                "max_bytes": max(1, int(values["max_bytes"])),
            }
            if not 0 <= config["start_hour"] <= 23 or not 0 <= config["end_hour"] <= 23:
                raise ValueError
            if not config["folders"] or not config["extensions"]:
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            json_response(self, 400, {"error": "invalid configuration"})
            return
        with LOCK:
            device["config"] = config
            save_state()
        json_response(self, 200, config)


def main():
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
