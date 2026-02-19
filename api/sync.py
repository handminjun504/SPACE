"""
POST /api/sync - 동기화 실행 (배경색 + 취소선 적용)

Request:  { "termination_url": "...", "worksheet_name": "Sheet1", "selected_indices": [0,1,3,...] }
Header:   X-Password: ...
Response: { "ok": true, "updated": 3, "skipped": 1 }

selected_indices: 미리보기 결과에서 사용자가 선택한 항목의 인덱스 (results 배열 기준)
                  비어있으면 정확매칭 + 검증통과 건만 자동 적용
"""

import json
import os
from http.server import BaseHTTPRequestHandler

import pandas as pd

from _lib.sheets import (
    extract_sheet_id,
    find_column_index,
    get_gspread_client,
    get_worksheet,
    highlight_rows,
    read_sheet_as_dataframe,
)
from _lib.matcher import (
    ALREADY_TERMINATED_KEYWORDS,
    TERMINATION_NOTE_TEMPLATE,
    MatchStatus,
    get_col,
    run_matching,
)


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

            # 데이터 읽기
            settlement_df = read_sheet_as_dataframe(client, settlement_id, settlement_ws)
            termination_df = read_sheet_as_dataframe(client, termination_id, ws_name)

            # 매칭 실행
            results = run_matching(settlement_df, termination_df)

            # 적용 대상 필터링
            if selected_indices is not None:
                # 사용자가 선택한 항목만
                targets = [results[i] for i in selected_indices if i < len(results)]
            else:
                # 기본: 정확매칭 또는 검증통과 유사매칭만
                targets = [
                    r for r in results
                    if r.settlement_index is not None
                    and (r.status == MatchStatus.EXACT
                         or (r.status == MatchStatus.FUZZY and r.verification_passed))
                ]

            # 정산 시트 워크시트 열기
            worksheet = get_worksheet(client, settlement_id, settlement_ws)

            # 비고 컬럼 인덱스 찾기
            note_col_idx = find_column_index(worksheet, "계약변경/해지/비고")

            # 계약종료일자 컬럼 인덱스
            try:
                end_date_col_idx = find_column_index(worksheet, "계약종료일자")
            except ValueError:
                end_date_col_idx = None

            # 업데이트 대상 준비
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

                # 이미 종료 처리된 건 건너뛰기
                existing_note = get_col(r.settlement_row, "계약변경/해지/비고")
                if any(kw in existing_note for kw in ALREADY_TERMINATED_KEYWORDS):
                    skipped += 1
                    continue

                # 만기일 가져오기
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

            # 강조 + 비고 적용
            if row_numbers:
                highlight_rows(
                    worksheet=worksheet,
                    row_numbers=row_numbers,
                    note_col_idx=note_col_idx,
                    notes=notes,
                    end_date_col_idx=end_date_col_idx,
                    end_dates=end_dates,
                )

            self._json(200, {
                "ok": True,
                "updated": updated,
                "skipped": skipped,
                "message": f"{updated}건 강조 처리 완료 (건너뜀: {skipped}건)",
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

