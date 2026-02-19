"""
POST /api/auth - 비밀번호 인증

Request:  { "password": "..." }
Response: { "ok": true } 또는 { "ok": false, "error": "..." }
"""

import json
import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}

            password = body.get("password", "")
            correct = os.environ.get("APP_PASSWORD", "")

            if not correct:
                self._json(500, {"ok": False, "error": "서버에 APP_PASSWORD가 설정되지 않았습니다."})
                return

            if password == correct:
                self._json(200, {"ok": True})
            else:
                self._json(401, {"ok": False, "error": "비밀번호가 일치하지 않습니다."})

        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Password")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Password")
        self.end_headers()

