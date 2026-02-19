"""
POST /api/preview - 매칭 미리보기 (dry-run)
최적화: 스프레드시트당 API 호출 1회, pandas 제거
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

from _lib.sheets import extract_sheet_id, get_gspread_client, open_and_read
from _lib.matcher import (
    run_matching, results_to_json, summary,
    detect_changes, changes_to_json, changes_summary,
)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        t0 = time.time()
        try:
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

            try:
                termination_id = extract_sheet_id(termination_url)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return

            client = get_gspread_client()
            print(f"[PREVIEW] 클라이언트 생성 ({time.time()-t0:.1f}s)")

            # 정산 시트: 한 번의 open으로 제목 + 탭목록 + 데이터 모두 가져옴
            try:
                settlement_title, _, settlement_data = open_and_read(
                    client, settlement_id, settlement_ws
                )
            except Exception as e:
                sa_email = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}")).get("client_email", "???")
                self._json(500, {"ok": False, "error": f"❌ 정산 시트 읽기 실패!\n서비스 계정: {sa_email}\n오류: {type(e).__name__}: {str(e)}\n\n→ 정산 시트를 서비스 계정에 '편집자' 권한으로 공유해주세요."})
                return
            print(f"[PREVIEW] 정산 시트: {settlement_title} ({len(settlement_data)}행) ({time.time()-t0:.1f}s)")

            # 종료 시트: 한 번의 open으로 모두 가져옴
            try:
                termination_title, termination_worksheets, termination_data = open_and_read(
                    client, termination_id, ws_name
                )
            except Exception as e:
                sa_email = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}")).get("client_email", "???")
                self._json(500, {"ok": False, "error": f"❌ 종료 시트 읽기 실패!\n서비스 계정: {sa_email}\n오류: {type(e).__name__}: {str(e)}\n\n→ 종료 시트를 서비스 계정에 '뷰어' 이상 권한으로 공유해주세요."})
                return
            print(f"[PREVIEW] 종료 시트: {termination_title} ({len(termination_data)}행) ({time.time()-t0:.1f}s)")

            if not termination_data:
                self._json(400, {"ok": False, "error": f"종료 시트 '{ws_name}' 탭에 데이터가 없습니다."})
                return

            # 매칭 실행 (dict 리스트 기반, pandas 불필요)
            results = run_matching(settlement_data, termination_data)
            summary_data = summary(results)
            print(f"[PREVIEW] 매칭 완료: {summary_data} ({time.time()-t0:.1f}s)")

            # 매칭된 건만 응답에 포함 (8,329건 → 11건 등으로 응답 크기 대폭 축소)
            matched_results = [r for r in results if r.settlement_index is not None]
            results_json = results_to_json(matched_results)

            # 내용 변경 감지 (매칭된 건에서 필드 차이 비교)
            changes = detect_changes(matched_results)
            changes_json = changes_to_json(changes)
            changes_sum = changes_summary(changes)
            print(f"[PREVIEW] 변경 감지: {changes_sum} ({time.time()-t0:.1f}s)")

            self._json(200, {
                "ok": True,
                "settlement_title": settlement_title,
                "termination_title": termination_title,
                "termination_worksheets": termination_worksheets,
                "settlement_count": len(settlement_data),
                "termination_count": len(termination_data),
                "summary": summary_data,
                "results": results_json,
                "changes": changes_json,
                "changes_summary": changes_sum,
                "elapsed_seconds": round(time.time() - t0, 1),
            })

        except Exception as e:
            import traceback
            print(f"[PREVIEW ERROR] {traceback.format_exc()}")
            self._json(500, {"ok": False, "error": f"서버 오류: {type(e).__name__}: {str(e)}"})

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
