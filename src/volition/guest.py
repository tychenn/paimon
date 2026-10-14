"""Authenticated guest bridge with durable action deduplication."""

import argparse
import hashlib
import hmac
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer

from pydantic import Field

from volition.contracts import Action, StrictModel, VolitionError
from volition.runtime import runner_lock


class LoopbackHTTPServer(HTTPServer):
    def server_bind(self):
        # HTTPServer normally reverse-resolves its address. A local guest bridge
        # has no need for DNS, which can block startup on otherwise offline Macs.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class ActionRequest(StrictModel):
    operation_id: str = Field(min_length=1, max_length=100)
    environment_id: str = Field(min_length=1, max_length=100)
    observation_id: str = Field(min_length=1, max_length=100)
    action: Action


class GuestService:
    def __init__(self, adapter, directory: Path):
        self.adapter = adapter
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "actions.sqlite3", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS actions "
            "(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT)"
        )
        (directory / "actions.sqlite3").chmod(0o600)

    def close(self):
        self.db.close()

    def observe(self):
        return self.adapter.observe().model_dump()

    def execute(self, request: ActionRequest):
        if request.environment_id != self.adapter.environment_id:
            raise VolitionError("环境身份不匹配。")
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        row = self.db.execute(
            "SELECT fingerprint, result FROM actions WHERE id=?", (request.operation_id,)
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise VolitionError("同一操作 ID 的请求内容已改变。")
            if row[1] is None:
                raise VolitionError("此前操作结果未知；拒绝再次执行。")
            return json.loads(row[1])
        self.adapter.validate(request)
        # Commit the intent BEFORE interacting with the desktop. A crash leaves an
        # unknown result, never an invitation to replay a possibly completed action.
        self.db.execute(
            "INSERT INTO actions VALUES (?, ?, NULL)", (request.operation_id, fingerprint)
        )
        result = self.adapter.execute(request)
        response = {"operation_id": request.operation_id, "status": "completed", "result": result}
        self.db.execute(
            "UPDATE actions SET result=? WHERE id=?",
            (json.dumps(response, ensure_ascii=False), request.operation_id),
        )
        return response


def handler_for(service: GuestService, key: str):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass  # Request bodies and desktop contents remain out of HTTP access logs.

        def send_json(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            valid = hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {key}".encode()
            )
            if not valid:
                self.send_json(401, {"error": "authentication_required"})
            return valid

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/v1/observation":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                self.send_json(200, service.observe())
            except Exception as exc:
                self.send_json(503, {"error": type(exc).__name__})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/v1/actions":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    self.send_json(413, {"error": "invalid_body_size"})
                    return
                request = ActionRequest.model_validate_json(self.rfile.read(length))
            except ValueError:
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                self.send_json(200, service.execute(request))
            except VolitionError as exc:
                self.send_json(409, {"error": str(exc)})
            except Exception as exc:
                self.send_json(500, {"error": type(exc).__name__, "outcome": "unknown"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("runtime/guest"))
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    # This guard executes before importing native desktop APIs or reading credentials.
    from volition.guest_macos import MacOSAdapter, require_virtual_mac

    try:
        require_virtual_mac()
        key = args.key_file.read_text().strip()
        if len(key) < 32 or args.key_file.stat().st_mode & 0o077:
            raise VolitionError("桥接密钥文件应至少包含 32 字符，且仅当前用户可读写。")
        with runner_lock(args.state_dir):
            service = GuestService(MacOSAdapter(args.environment_id), args.state_dir)
            try:
                with LoopbackHTTPServer(
                    ("127.0.0.1", args.port), handler_for(service, key)
                ) as server:
                    print(
                        json.dumps(
                            {
                                "listening": server.server_address,
                                "environment_id": args.environment_id,
                            }
                        ),
                        flush=True,
                    )
                    server.serve_forever()
            finally:
                service.close()
    except (VolitionError, OSError, ImportError) as exc:
        print(f"Guest bridge 未启动：{type(exc).__name__}: {exc}")
        return 2
    except KeyboardInterrupt:
        return 0
    return 0
