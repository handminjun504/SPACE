"""
GET /api/health - 환경변수 및 시스템 상태 확인
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        checks = {}

        # 1) APP_PASSWORD
        pw = os.environ.get("APP_PASSWORD", "")
        checks["APP_PASSWORD"] = "✅ 설정됨" if pw else "❌ 미설정"

        # 2) SETTLEMENT_SHEET_ID
        sid = os.environ.get("SETTLEMENT_SHEET_ID", "")
        checks["SETTLEMENT_SHEET_ID"] = f"✅ 설정됨 ({sid[:8]}...)" if sid else "❌ 미설정"

        # 3) SETTLEMENT_WORKSHEET_NAME
        ws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "")
        checks["SETTLEMENT_WORKSHEET_NAME"] = f"✅ {ws}" if ws else "⚠️ 미설정 (기본값 Sheet1 사용)"

        # 4) GOOGLE_CREDENTIALS_JSON
        creds = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
        if not creds:
            checks["GOOGLE_CREDENTIALS_JSON"] = "❌ 미설정"
        else:
            try:
                parsed = json.loads(creds)
                email = parsed.get("client_email", "???")
                checks["GOOGLE_CREDENTIALS_JSON"] = f"✅ 설정됨 (서비스계정: {email})"
            except json.JSONDecodeError as e:
                checks["GOOGLE_CREDENTIALS_JSON"] = f"❌ JSON 파싱 실패: {str(e)[:80]}"

        # 5) _lib 모듈 import 테스트
        sys.path.insert(0, os.path.dirname(__file__))
        try:
            from _lib import sheets, matcher
            checks["_lib 모듈"] = "✅ import 성공"
        except ImportError as e:
            checks["_lib 모듈"] = f"❌ import 실패: {str(e)}"

        # 6) 의존성 패키지
        for pkg in ["gspread", "pandas", "rapidfuzz", "google.oauth2"]:
            try:
                __import__(pkg)
                checks[f"패키지:{pkg}"] = "✅ 설치됨"
            except ImportError:
                checks[f"패키지:{pkg}"] = "❌ 미설치"

        # 7) Python 버전
        checks["Python"] = sys.version

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(checks, ensure_ascii=False, indent=2).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

