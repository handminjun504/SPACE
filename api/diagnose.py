"""
GET /api/diagnose - 단계별 격리 진단
쿼리 파라미터:
  ?mode=settlement  → 정산 시트만 테스트 (기본)
  ?mode=termination&url=...  → 종료 시트만 테스트
  ?mode=full&url=...  → 전체 플로우 테스트
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        t0 = time.time()
        steps = []

        def elapsed():
            return round(time.time() - t0, 3)

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        mode = qs.get("mode", ["settlement"])[0]
        term_url = qs.get("url", [""])[0]
        term_ws = qs.get("ws", ["계약 종료"])[0]

        # Step 1: Import
        try:
            from _lib.sheets import get_gspread_client, extract_sheet_id, open_and_read
            from _lib.matcher import run_matching, results_to_json, summary
            from _lib.matcher import detect_changes, changes_to_json, changes_summary
            steps.append({"step": "1_imports", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            import traceback
            steps.append({"step": "1_imports", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
            return self._respond(steps, t0, mode)

        # Step 2: Client
        try:
            client = get_gspread_client()
            steps.append({"step": "2_client", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "2_client", "ok": False, "error": str(e), "elapsed_s": elapsed()})
            return self._respond(steps, t0, mode)

        # mode=settlement (기본): 정산 시트만
        if mode in ("settlement", "full"):
            sid = os.environ.get("SETTLEMENT_SHEET_ID", "")
            sws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "기초데이터")
            try:
                s_title, _, s_data = open_and_read(client, sid, sws)
                steps.append({"step": "3_settlement", "ok": True, "title": s_title, "rows": len(s_data), "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "3_settlement", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
                return self._respond(steps, t0, mode)

        # mode=termination: 종료 시트만
        if mode in ("termination", "full") and term_url:
            try:
                term_id = extract_sheet_id(term_url)
                t_title, t_ws_list, t_data = open_and_read(client, term_id, term_ws)
                steps.append({"step": "4_termination", "ok": True, "title": t_title, "ws_list": t_ws_list, "rows": len(t_data), "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "4_termination", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
                return self._respond(steps, t0, mode)

        # mode=full: 매칭 + 변경감지
        if mode == "full" and term_url:
            try:
                results = run_matching(s_data, t_data)
                matched = sum(1 for r in results if r.settlement_index is not None)
                steps.append({"step": "5_matching", "ok": True, "total": len(results), "matched": matched, "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "5_matching", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
                return self._respond(steps, t0, mode)

            try:
                changes = detect_changes(results)
                rj = results_to_json(results)
                cj = changes_to_json(changes)
                resp_size = len(json.dumps({"r": rj, "c": cj}, ensure_ascii=False))
                steps.append({"step": "6_serialize", "ok": True, "changes": len(changes), "resp_bytes": resp_size, "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "6_serialize", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})

        self._respond(steps, t0, mode)

    def _respond(self, steps, t0, mode="?"):
        result = {"mode": mode, "steps": steps, "total_s": round(time.time() - t0, 3), "py": sys.version[:10]}
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
