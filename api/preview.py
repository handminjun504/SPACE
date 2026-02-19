"""
POST /api/preview - 매칭 미리보기 (dry-run) v4
GET  /api/preview - 버전 및 상태 확인
"""

# region agent log
_PREVIEW_VERSION = "v6-room-primary"
import time as _time
_t_module_start = _time.time()
# endregion

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

# region agent log
_t_pre_import = _time.time()
# endregion

from _lib.sheets import extract_sheet_id, get_gspread_client, open_and_read
from _lib.matcher import (
    run_matching, results_to_json, summary,
    detect_changes, changes_to_json, changes_summary,
)

# region agent log
_t_imports_done = _time.time()
_import_duration = round(_t_imports_done - _t_module_start, 3)
print(f"[PREVIEW] module loaded: version={_PREVIEW_VERSION}, imports={_import_duration}s")
# endregion


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        """버전 및 배포 상태 확인 (디버그용)"""
        # region agent log
        self._json(200, {
            "ok": True,
            "version": _PREVIEW_VERSION,
            "import_duration_s": _import_duration,
            "message": "preview endpoint ready",
        })
        # endregion

    def do_POST(self):
        t0 = _time.time()
        # region agent log
        print(f"[PREVIEW {_PREVIEW_VERSION}] POST 시작")
        _log_path = "/tmp/preview_debug.log"
        def _dlog(step, data=None):
            try:
                entry = json.dumps({"step": step, "elapsed_s": round(_time.time()-t0, 3), "data": data or {}}, ensure_ascii=False)
                print(f"[PREVIEW {_PREVIEW_VERSION}] {entry}")
                with open(_log_path, "a") as f:
                    f.write(entry + "\n")
            except Exception:
                pass
        _dlog("start")
        # endregion

        try:
            pw = self.headers.get("X-Password", "")
            if pw != os.environ.get("APP_PASSWORD", ""):
                # region agent log
                _dlog("auth_fail")
                # endregion
                self._json(401, {"ok": False, "error": "인증 실패", "_v": _PREVIEW_VERSION})
                return

            # region agent log
            _dlog("auth_ok")
            # endregion

            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}

            termination_url = body.get("termination_url", "")
            ws_name = body.get("worksheet_name", "계약 종료")

            if not termination_url:
                self._json(400, {"ok": False, "error": "종료 시트 URL을 입력해주세요."})
                return

            settlement_id = os.environ.get("SETTLEMENT_SHEET_ID", "")
            settlement_ws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "기초데이터")
            if not settlement_id:
                self._json(500, {"ok": False, "error": "SETTLEMENT_SHEET_ID 환경변수가 설정되지 않았습니다."})
                return

            try:
                termination_id = extract_sheet_id(termination_url)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return

            client = get_gspread_client()
            # region agent log
            _dlog("client_ready")
            # endregion

            try:
                settlement_title, _, settlement_data = open_and_read(
                    client, settlement_id, settlement_ws
                )
            except Exception as e:
                sa_email = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}")).get("client_email", "???")
                self._json(500, {"ok": False, "error": f"❌ 정산 시트 읽기 실패!\n서비스 계정: {sa_email}\n오류: {type(e).__name__}: {str(e)}\n\n→ 정산 시트를 서비스 계정에 '편집자' 권한으로 공유해주세요."})
                return
            # region agent log
            _dlog("settlement_read", {"rows": len(settlement_data)})
            # endregion

            try:
                termination_title, termination_worksheets, termination_data = open_and_read(
                    client, termination_id, ws_name
                )
            except Exception as e:
                sa_email = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}")).get("client_email", "???")
                self._json(500, {"ok": False, "error": f"❌ 종료 시트 읽기 실패!\n서비스 계정: {sa_email}\n오류: {type(e).__name__}: {str(e)}\n\n→ 종료 시트를 서비스 계정에 '뷰어' 이상 권한으로 공유해주세요."})
                return
            # region agent log
            _dlog("termination_read", {"rows": len(termination_data)})
            # endregion

            if not termination_data:
                self._json(400, {"ok": False, "error": f"종료 시트 '{ws_name}' 탭에 데이터가 없습니다."})
                return

            results = run_matching(settlement_data, termination_data)
            summary_data = summary(results)
            # region agent log
            _dlog("matching_done", {"summary": summary_data})
            # endregion

            matched_results = [r for r in results if r.settlement_index is not None]
            results_json = results_to_json(matched_results)

            changes = detect_changes(matched_results)
            changes_json = changes_to_json(changes)
            changes_sum = changes_summary(changes)
            # region agent log
            _dlog("changes_done", {"changes": changes_sum})
            # endregion

            self._json(200, {
                "ok": True,
                "_v": _PREVIEW_VERSION,
                "settlement_title": settlement_title,
                "termination_title": termination_title,
                "termination_worksheets": termination_worksheets,
                "settlement_count": len(settlement_data),
                "termination_count": len(termination_data),
                "summary": summary_data,
                "results": results_json,
                "changes": changes_json,
                "changes_summary": changes_sum,
                "elapsed_seconds": round(_time.time() - t0, 1),
            })
            # region agent log
            _dlog("response_sent")
            # endregion

        except Exception as e:
            import traceback
            # region agent log
            _dlog("error", {"type": type(e).__name__, "msg": str(e)})
            # endregion
            print(f"[PREVIEW ERROR] {traceback.format_exc()}")
            self._json(500, {"ok": False, "error": f"서버 오류: {type(e).__name__}: {str(e)}", "_v": _PREVIEW_VERSION})

    def _json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Password")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Password")
        self.end_headers()
