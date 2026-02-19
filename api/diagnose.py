"""
GET /api/diagnose - 단계별 타이밍 진단
각 단계의 소요시간을 측정하여 504 병목을 찾습니다.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

SAFE_LIMIT = 50  # 60초 타임아웃 전에 안전하게 응답


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        t0 = time.time()
        steps = []

        def elapsed():
            return round(time.time() - t0, 3)

        def remaining():
            return SAFE_LIMIT - (time.time() - t0)

        # Step 1: Import 테스트
        try:
            from _lib.sheets import get_gspread_client
            import gspread
            from rapidfuzz import fuzz
            steps.append({"step": "1_imports", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "1_imports", "ok": False, "error": str(e), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 5:
            steps.append({"step": "TIMEOUT_RISK", "remaining_s": round(remaining(), 1)})
            return self._respond(steps, t0)

        # Step 2: gspread 클라이언트 생성 (Google 인증)
        try:
            client = get_gspread_client()
            steps.append({"step": "2_gspread_client", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "2_gspread_client", "ok": False, "error": str(e), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 10:
            steps.append({"step": "TIMEOUT_RISK", "remaining_s": round(remaining(), 1)})
            return self._respond(steps, t0)

        # Step 3: 정산 시트 열기 (메타데이터만)
        sid = os.environ.get("SETTLEMENT_SHEET_ID", "")
        sws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "Sheet1")
        try:
            spreadsheet = client.open_by_key(sid)
            title = spreadsheet.title
            ws_list = [w.title for w in spreadsheet.worksheets()]
            steps.append({
                "step": "3_settlement_open",
                "ok": True,
                "title": title,
                "worksheets": ws_list,
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "3_settlement_open", "ok": False, "error": f"{type(e).__name__}: {str(e)}", "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 15:
            steps.append({"step": "TIMEOUT_RISK", "remaining_s": round(remaining(), 1)})
            return self._respond(steps, t0)

        # Step 4: 정산 시트 데이터 읽기
        try:
            worksheet = spreadsheet.worksheet(sws)
            all_values = worksheet.get_all_values()
            rows = len(all_values) - 1 if all_values else 0
            cols = len(all_values[0]) if all_values else 0
            # 첫 행(헤더) 샘플
            headers_sample = all_values[0][:10] if all_values else []
            steps.append({
                "step": "4_settlement_read",
                "ok": True,
                "rows": rows,
                "cols": cols,
                "headers_sample": headers_sample,
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "4_settlement_read", "ok": False, "error": f"{type(e).__name__}: {str(e)}", "elapsed_s": elapsed()})

        self._respond(steps, t0)

    def _respond(self, steps, t0):
        result = {
            "steps": steps,
            "total_elapsed_s": round(time.time() - t0, 3),
            "maxDuration_config": 60,
            "python_version": sys.version,
        }
        body = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

