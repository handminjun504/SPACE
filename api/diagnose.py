"""
GET /api/diagnose - 전체 Preview 플로우 단계별 타이밍 진단
종료 시트 URL은 쿼리 파라미터로: ?url=...&ws=계약 종료
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))

SAFE_LIMIT = 110  # 120초 타임아웃 전에 안전하게 응답


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        t0 = time.time()
        steps = []

        def elapsed():
            return round(time.time() - t0, 3)

        def remaining():
            return SAFE_LIMIT - (time.time() - t0)

        # 쿼리 파라미터 파싱
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        term_url = qs.get("url", [""])[0]
        term_ws = qs.get("ws", ["계약 종료"])[0]

        # Step 1: Import 테스트
        try:
            from _lib.sheets import get_gspread_client, extract_sheet_id, open_and_read
            from _lib.matcher import run_matching, results_to_json, summary
            from _lib.matcher import detect_changes, changes_to_json, changes_summary
            steps.append({"step": "1_imports", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            import traceback
            steps.append({"step": "1_imports", "ok": False, "error": str(e), "traceback": traceback.format_exc(), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 5:
            return self._respond(steps, t0, "TIMEOUT_RISK after imports")

        # Step 2: gspread 클라이언트 생성
        try:
            client = get_gspread_client()
            steps.append({"step": "2_gspread_client", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "2_gspread_client", "ok": False, "error": str(e), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 10:
            return self._respond(steps, t0, "TIMEOUT_RISK after client")

        # Step 3: 정산 시트 읽기
        sid = os.environ.get("SETTLEMENT_SHEET_ID", "")
        sws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "기초데이터")
        try:
            s_title, s_ws_list, s_data = open_and_read(client, sid, sws)
            steps.append({
                "step": "3_settlement_read",
                "ok": True,
                "title": s_title,
                "rows": len(s_data),
                "elapsed_s": elapsed(),
            })
        except Exception as e:
            import traceback
            steps.append({"step": "3_settlement_read", "ok": False, "error": str(e), "traceback": traceback.format_exc(), "elapsed_s": elapsed()})
            return self._respond(steps, t0)

        if remaining() < 15:
            return self._respond(steps, t0, "TIMEOUT_RISK after settlement")

        # Step 4: 종료 시트 읽기 (URL이 있는 경우만)
        t_data = []
        if term_url:
            try:
                term_id = extract_sheet_id(term_url)
                t_title, t_ws_list, t_data = open_and_read(client, term_id, term_ws)
                steps.append({
                    "step": "4_termination_read",
                    "ok": True,
                    "title": t_title,
                    "worksheets": t_ws_list,
                    "rows": len(t_data),
                    "elapsed_s": elapsed(),
                })
            except Exception as e:
                import traceback
                steps.append({"step": "4_termination_read", "ok": False, "error": str(e), "traceback": traceback.format_exc(), "elapsed_s": elapsed()})
                return self._respond(steps, t0)
        else:
            steps.append({"step": "4_termination_read", "skipped": True, "reason": "url param missing"})

        if remaining() < 20:
            return self._respond(steps, t0, "TIMEOUT_RISK after termination")

        # Step 5: 매칭 실행
        if t_data:
            try:
                results = run_matching(s_data, t_data)
                matched = sum(1 for r in results if r.settlement_index is not None)
                steps.append({
                    "step": "5_matching",
                    "ok": True,
                    "total": len(results),
                    "matched": matched,
                    "elapsed_s": elapsed(),
                })
            except Exception as e:
                import traceback
                steps.append({"step": "5_matching", "ok": False, "error": str(e), "traceback": traceback.format_exc(), "elapsed_s": elapsed()})
                return self._respond(steps, t0)
        else:
            steps.append({"step": "5_matching", "skipped": True})

        if remaining() < 10:
            return self._respond(steps, t0, "TIMEOUT_RISK after matching")

        # Step 6: 내용 변경 감지
        if t_data:
            try:
                changes = detect_changes(results)
                steps.append({
                    "step": "6_detect_changes",
                    "ok": True,
                    "changes_count": len(changes),
                    "elapsed_s": elapsed(),
                })
            except Exception as e:
                import traceback
                steps.append({"step": "6_detect_changes", "ok": False, "error": str(e), "traceback": traceback.format_exc(), "elapsed_s": elapsed()})
                return self._respond(steps, t0)
        else:
            steps.append({"step": "6_detect_changes", "skipped": True})

        # Step 7: JSON 직렬화
        if t_data:
            try:
                results_json = results_to_json(results)
                summary_data = summary(results)
                changes_json = changes_to_json(changes)
                changes_sum = changes_summary(changes)
                response_size = len(json.dumps({
                    "results": results_json,
                    "changes": changes_json,
                    "summary": summary_data,
                    "changes_summary": changes_sum,
                }, ensure_ascii=False))
                steps.append({
                    "step": "7_json_serialize",
                    "ok": True,
                    "response_size_bytes": response_size,
                    "elapsed_s": elapsed(),
                })
            except Exception as e:
                import traceback
                steps.append({"step": "7_json_serialize", "ok": False, "error": str(e), "traceback": traceback.format_exc(), "elapsed_s": elapsed()})

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
