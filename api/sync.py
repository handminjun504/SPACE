"""
POST /api/sync - 동기화 실행 (배경색 + 취소선 적용)
최적화: open_and_read로 API 호출 최소화, pandas 제거
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

from _lib.sheets import (
    extract_sheet_id,
    find_column_index,
    get_gspread_client,
    get_worksheet,
    highlight_rows,
    open_and_read,
)
from _lib.matcher import (
    ALREADY_TERMINATED_KEYWORDS,
    TERMINATION_NOTE_TEMPLATE,
    MatchStatus,
    get_col,
    run_matching,
    detect_changes,
)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            pw = self.headers.get("X-Password", "")
            if pw != os.environ.get("APP_PASSWORD", ""):
                self._json(401, {"ok": False, "error": "인증 실패"})
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}

            termination_url = body.get("termination_url", "")
            ws_name = body.get("worksheet_name", "Sheet1")
            selected_indices = body.get("selected_indices", None)

            if not termination_url:
                self._json(400, {"ok": False, "error": "종료 시트 URL을 입력해주세요."})
                return

            settlement_id = os.environ.get("SETTLEMENT_SHEET_ID", "")
            settlement_ws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "Sheet1")
            if not settlement_id:
                self._json(500, {"ok": False, "error": "SETTLEMENT_SHEET_ID 환경변수 미설정"})
                return

            termination_id = extract_sheet_id(termination_url)
            client = get_gspread_client()

            # 데이터 읽기 (최적화: 한 번에)
            _, _, settlement_data = open_and_read(client, settlement_id, settlement_ws)
            _, _, termination_data = open_and_read(client, termination_id, ws_name)

            # 매칭 실행
            results = run_matching(settlement_data, termination_data)

            # 적용 대상 필터링
            if selected_indices is not None:
                targets = [results[i] for i in selected_indices if i < len(results)]
            else:
                targets = [
                    r for r in results
                    if r.settlement_index is not None
                    and (r.status == MatchStatus.EXACT
                         or (r.status == MatchStatus.FUZZY and r.verification_passed))
                ]

            # 정산 시트 워크시트 열기 (쓰기용)
            worksheet = get_worksheet(client, settlement_id, settlement_ws)
            note_col_idx = find_column_index(worksheet, "계약변경/해지/비고")

            try:
                end_date_col_idx = find_column_index(worksheet, "계약종료일자")
            except ValueError:
                end_date_col_idx = None

            row_numbers = []
            notes = []
            end_dates = []
            updated = 0
            skipped = 0

            for r in targets:
                if r.settlement_index is None:
                    skipped += 1
                    continue

                sheet_row = r.settlement_index + 2  # 0-based → 1-based + 헤더

                existing_note = get_col(r.settlement_row, "계약변경/해지/비고")
                if any(kw in existing_note for kw in ALREADY_TERMINATED_KEYWORDS):
                    skipped += 1
                    continue

                expiry = get_col(r.termination_row, "계약 만기 날짜")
                new_note = TERMINATION_NOTE_TEMPLATE.format(
                    expiry_date=expiry if expiry else "미확인"
                )
                if existing_note.strip():
                    new_note = f"{existing_note} | {new_note}"

                row_numbers.append(sheet_row)
                notes.append(new_note)
                end_dates.append(expiry)
                updated += 1

            if row_numbers:
                highlight_rows(
                    worksheet=worksheet,
                    row_numbers=row_numbers,
                    note_col_idx=note_col_idx,
                    notes=notes,
                    end_date_col_idx=end_date_col_idx,
                    end_dates=end_dates,
                )

            # 내용 변경 감지 건수
            changes = detect_changes(results)
            change_count = len(changes)

            self._json(200, {
                "ok": True,
                "updated": updated,
                "skipped": skipped,
                "change_count": change_count,
                "message": f"{updated}건 종료 처리 완료 (건너뜀: {skipped}건)" + (f" · 내용 변경 {change_count}건 감지됨" if change_count > 0 else ""),
            })

        except Exception as e:
            import traceback
            print(f"[SYNC ERROR] {traceback.format_exc()}")
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
