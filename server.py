#!/usr/bin/env python3

import base64
import binascii
import hmac
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/data/measurements.db")
API_TOKEN = os.getenv("API_TOKEN", "")
WEB_USERNAME = os.getenv("WEB_USERNAME", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
INDEX_HTML = Path(__file__).with_name("index.html").read_bytes()
MAX_BODY_BYTES = 16 * 1024


class ClientError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_timestamp(value):
    if value is None:
        return utc_now()
    if not isinstance(value, str) or len(value) > 40:
        raise ClientError("measured_at must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ClientError("measured_at must be a valid ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ClientError("measured_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_text(payload, name, max_length):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ClientError(f"{name} must be a non-empty string up to {max_length} characters")
    if any(ord(character) < 32 for character in value):
        raise ClientError(f"{name} contains control characters")
    return value.strip()


def require_integer(payload, name, minimum, maximum):
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ClientError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def validate_measurement(payload):
    if not isinstance(payload, dict):
        raise ClientError("request body must be a JSON object")
    allowed = {"measurement_id", "device_id", "measured_at", "weight_g", "impedance_raw"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ClientError(f"unknown fields: {', '.join(unknown)}")

    measurement_id = payload.get("measurement_id")
    if measurement_id is None:
        measurement_id = uuid.uuid4().hex
    else:
        measurement_id = require_text(payload, "measurement_id", 128)

    return {
        "measurement_id": measurement_id,
        "device_id": require_text(payload, "device_id", 64),
        "measured_at": normalize_timestamp(payload.get("measured_at")),
        "received_at": utc_now(),
        "weight_g": require_integer(payload, "weight_g", 1, 500_000),
        "impedance_raw": require_integer(payload, "impedance_raw", 0, 65_535),
    }


def connect_database():
    database = sqlite3.connect(DB_PATH, timeout=5)
    database.row_factory = sqlite3.Row
    return database


def initialize_database():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect_database() as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS measurements (
                measurement_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                measured_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                weight_g INTEGER NOT NULL CHECK (weight_g BETWEEN 1 AND 500000),
                impedance_raw INTEGER NOT NULL CHECK (impedance_raw BETWEEN 0 AND 65535)
            )
            """
        )
        database.execute(
            "CREATE INDEX IF NOT EXISTS measurements_measured_at ON measurements(measured_at DESC)"
        )


def save_measurement(measurement):
    columns = (
        "measurement_id",
        "device_id",
        "measured_at",
        "received_at",
        "weight_g",
        "impedance_raw",
    )
    values = tuple(measurement[column] for column in columns)
    with connect_database() as database:
        cursor = database.execute(
            f"INSERT OR IGNORE INTO measurements ({', '.join(columns)}) VALUES (?, ?, ?, ?, ?, ?)",
            values,
        )
        row = database.execute(
            "SELECT * FROM measurements WHERE measurement_id = ?", (measurement["measurement_id"],)
        ).fetchone()

    saved = dict(row)
    if cursor.rowcount == 0:
        comparable = ("device_id", "measured_at", "weight_g", "impedance_raw")
        if any(saved[field] != measurement[field] for field in comparable):
            raise ClientError("measurement_id already exists with different data", 409)
    return saved, cursor.rowcount == 1


def list_measurements(limit):
    with connect_database() as database:
        rows = database.execute(
            """
            SELECT measurement_id, device_id, measured_at, received_at, weight_g, impedance_raw
            FROM measurements
            ORDER BY measured_at DESC, received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = database.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    return [dict(row) for row in rows], total


class ScaleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AFUScale/1.0"
    sys_version = ""

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def web_authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied, f"{WEB_USERNAME}:{WEB_PASSWORD}")

    def require_web_auth(self):
        if self.web_authorized():
            return True
        body = b"Authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AFU Scale", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return False

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ClientError("Content-Type must be application/json", 415)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as error:
            raise ClientError("Content-Length is required", 411) from error
        if length < 1:
            raise ClientError("request body is empty")
        if length > MAX_BODY_BYTES:
            raise ClientError("request body is too large", 413)
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ClientError("request body is not valid JSON") from error

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            try:
                with connect_database() as database:
                    database.execute("SELECT 1")
                self.send_json(200, {"status": "ok"})
            except sqlite3.Error:
                self.send_json(503, {"status": "unavailable"})
            return

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self.require_web_auth():
            return

        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(INDEX_HTML)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(INDEX_HTML)
            return

        if parsed.path == "/api/measurements":
            try:
                limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                if not 1 <= limit <= 500:
                    raise ValueError
            except ValueError:
                self.send_json(400, {"error": "limit must be an integer between 1 and 500"})
                return
            measurements, total = list_measurements(limit)
            self.send_json(200, {"measurements": measurements, "total": total})
            return

        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/measurements":
            self.send_json(404, {"error": "not found"})
            return
        if not API_TOKEN or not hmac.compare_digest(self.headers.get("X-API-Key", ""), API_TOKEN):
            self.send_json(401, {"error": "invalid API token"})
            return
        try:
            measurement = validate_measurement(self.read_json())
            saved, created = save_measurement(measurement)
        except ClientError as error:
            self.send_json(error.status, {"error": str(error)})
            return
        except sqlite3.Error:
            self.send_json(503, {"error": "database unavailable"})
            return
        self.send_json(201 if created else 200, {"created": created, "measurement": saved})


def main():
    missing = [name for name, value in (("API_TOKEN", API_TOKEN), ("WEB_PASSWORD", WEB_PASSWORD)) if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    if len(API_TOKEN) < 24 or len(WEB_PASSWORD) < 12:
        raise SystemExit("API_TOKEN must be at least 24 characters and WEB_PASSWORD at least 12")
    if not WEB_USERNAME or ":" in WEB_USERNAME:
        raise SystemExit("WEB_USERNAME must be non-empty and cannot contain ':'")

    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), ScaleHandler)
    server.daemon_threads = True
    print(f"AFU Scale listening on http://{HOST}:{PORT}; database={DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
