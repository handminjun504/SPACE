"""
GET /api/diagnose - 전체 파이프라인 타이밍 진단
정산 시트 + 종료 시트 + 매칭 + 변경감지까지 전부 테스트합니다.
"""

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

SAFE_LIMIT = 110  # 120초 maxDuration 중 안전 한계


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        t0 = time.time()
        steps = []

        def elapsed():
            return round(time.time() - t0, 3)

        def remaining():
            return SAFE_LIMIT - (time.time() - t0)

        # Step 1: Import 테스트 (matcher 포함)
        try:
            from _lib.sheets import get_gspread_client, extract_sheet_id, open_and_read
            from _lib.matcher import run_matching, results_to_json, summary
            from _lib.matcher import detect_changes, changes_to_json, changes_summary
            from rapidfuzz import fuzz
            steps.append({"step": "1_imports", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "1_imports", "ok": False, "error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc(), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 5:
            return self._respond(steps, t0, "TIMEOUT_RISK after imports")

        # Step 2: gspread 클라이언트 생성
        try:
            client = get_gspread_client()
            steps.append({"step": "2_client", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "2_client", "ok": False, "error": str(e), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 10:
            return self._respond(steps, t0, "TIMEOUT_RISK after client")

        # Step 3: 정산 시트 읽기
        sid = os.environ.get("SETTLEMENT_SHEET_ID", "")
        sws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "기초데이터")
        try:
            s_title, s_tabs, s_data = open_and_read(client, sid, sws)
            steps.append({
                "step": "3_settlement_read", "ok": True,
                "title": s_title, "rows": len(s_data),
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "3_settlement_read", "ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 15:
            return self._respond(steps, t0, "TIMEOUT_RISK after settlement")

        # Step 4: 종료 시트 읽기
        term_url = self.headers.get("X-Termination-Url", "")
        term_ws = self.headers.get("X-Termination-Ws", "계약 종료")
        if not term_url:
            steps.append({"step": "4_termination_read", "ok": False, "error": "X-Termination-Url 헤더 필요"})
            return self._respond(steps, t0)

        try:
            term_id = extract_sheet_id(term_url)
            t_title, t_tabs, t_data = open_and_read(client, term_id, term_ws)
            steps.append({
                "step": "4_termination_read", "ok": True,
                "title": t_title, "rows": len(t_data),
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "4_termination_read", "ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 20:
            return self._respond(steps, t0, "TIMEOUT_RISK after termination")

        # Step 5: 매칭 실행
        try:
            results = run_matching(s_data, t_data)
            matched = sum(1 for r in results if r.settlement_index is not None)
            sum_data = summary(results)
            steps.append({
                "step": "5_matching", "ok": True,
                "total": len(results), "matched": matched,
                "summary": sum_data,
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "5_matching", "ok": False, "error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc(), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        # Step 6: 변경 감지
        try:
            changes = detect_changes(results)
            ch_json = changes_to_json(changes)
            ch_sum = changes_summary(changes)
            steps.append({
                "step": "6_changes", "ok": True,
                "change_count": len(changes),
                "changes_summary": ch_sum,
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "6_changes", "ok": False, "error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc(), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        # Step 7: JSON 직렬화
        try:
            results_json = results_to_json(results)
            payload_size = len(json.dumps({"results": results_json, "changes": ch_json}, ensure_ascii=False))
            steps.append({
                "step": "7_serialize", "ok": True,
                "payload_bytes": payload_size,
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            steps.append({"step": "7_serialize", "ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_s": elapsed()})

        self._respond(steps, t0)

    def _respond(self, steps, t0, warning=None):
        result = {
            "steps": steps,
            "total_elapsed_s": round(time.time() - t0, 3),
            "maxDuration_config": 120,
            "python_version": sys.version,
        }
        if warning:
            result["warning"] = warning
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
