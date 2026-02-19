"""
POST /api/preview - 매칭 미리보기 (dry-run)

Request:  { "termination_url": "...", "worksheet_name": "Sheet1" }
Header:   X-Password: ...
Response: { "ok": true, "summary": {...}, "results": [...], "settlement_title": "..." }
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Vercel 런타임에서 _lib 모듈을 찾을 수 있도록 경로 추가
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd

from _lib.sheets import (
    extract_sheet_id,
    get_gspread_client,
    get_sheet_title,
    list_worksheets,
    read_sheet_as_dataframe,
)
from _lib.matcher import run_matching, results_to_json, summary


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # 비밀번호 확인
            pw = self.headers.get("X-Password", "")
            if pw != os.environ.get("APP_PASSWORD", ""):
                self._json(401, {"ok": False, "error": "인증 실패"})
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}

            termination_url = body.get("termination_url", "")
            ws_name = body.get("worksheet_name", "Sheet1")

            if not termination_url:
                self._json(400, {"ok": False, "error": "종료 시트 URL을 입력해주세요."})
                return

            settlement_id = os.environ.get("SETTLEMENT_SHEET_ID", "")
            settlement_ws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "Sheet1")
            if not settlement_id:
                self._json(500, {"ok": False, "error": "SETTLEMENT_SHEET_ID 환경변수가 설정되지 않았습니다."})
                return

            # 종료 시트 ID 추출
            try:
                termination_id = extract_sheet_id(termination_url)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return

            # Google Sheets 연결
            client = get_gspread_client()

            # 시트 제목 가져오기
            settlement_title = get_sheet_title(client, settlement_id)
            termination_title = get_sheet_title(client, termination_id)

            # 워크시트 목록
            termination_worksheets = list_worksheets(client, termination_id)

            # 데이터 읽기
            settlement_df = read_sheet_as_dataframe(client, settlement_id, settlement_ws)
            termination_df = read_sheet_as_dataframe(client, termination_id, ws_name)

            if termination_df.empty:
                self._json(400, {"ok": False, "error": f"종료 시트 '{ws_name}' 탭에 데이터가 없습니다."})
                return

            # 매칭 실행
            results = run_matching(settlement_df, termination_df)
            results_json = results_to_json(results)
            summary_data = summary(results)

            self._json(200, {
                "ok": True,
                "settlement_title": settlement_title,
                "termination_title": termination_title,
                "termination_worksheets": termination_worksheets,
                "settlement_count": len(settlement_df),
                "termination_count": len(termination_df),
                "summary": summary_data,
                "results": results_json,
            })

        except Exception as e:
            self._json(500, {"ok": False, "error": f"서버 오류: {str(e)}"})

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

