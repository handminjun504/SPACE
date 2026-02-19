"""
Google Sheets 읽기/쓰기/포맷팅 모듈 (Vercel 서버리스용)

서비스 계정 JSON은 환경변수 GOOGLE_CREDENTIALS_JSON에서 로드합니다.
최적화: 스프레드시트를 한 번만 열어 API 호출을 최소화합니다.
"""

import json
import os
import re
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# 종료 건 강조 색상 (연한 빨간색 배경)
HIGHLIGHT_BG_COLOR = {"red": 1.0, "green": 0.8, "blue": 0.8}


def get_gspread_client() -> gspread.Client:
    """환경변수에서 서비스 계정 인증 정보를 로드하여 gspread 클라이언트 반환"""
    creds_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json_str:
        raise ValueError("GOOGLE_CREDENTIALS_JSON 환경변수가 설정되지 않았습니다.")
    creds_info = json.loads(creds_json_str)
    credentials = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(credentials)


def extract_sheet_id(url_or_id: str) -> str:
    """구글 시트 URL 또는 ID에서 시트 ID를 추출합니다."""
    url_or_id = url_or_id.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    if re.match(r"^[a-zA-Z0-9_-]+$", url_or_id):
        return url_or_id
    raise ValueError(f"유효한 구글 시트 URL/ID가 아닙니다: {url_or_id}")


def _make_unique_headers(headers: list[str]) -> list[str]:
    """중복 헤더를 고유하게 만듭니다. 예: ['호실', '호실'] → ['호실', '호실_2']"""
    seen = {}
    result = []
    for h in headers:
        h = str(h).strip()
        if not h:
            h = "unnamed"
        if h in seen:
            seen[h] += 1
            result.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 1
            result.append(h)
    return result


def open_and_read(
    client: gspread.Client,
    sheet_id: str,
    worksheet_name: str = "Sheet1",
) -> tuple[str, list[str], list[dict]]:
    """
    스프레드시트를 한 번만 열어서 제목, 탭 목록, 데이터를 모두 반환합니다.
    API 호출을 최소화합니다.

    Returns:
        (시트 제목, 워크시트 목록, 데이터 딕셔너리 리스트)
    """
    spreadsheet = client.open_by_key(sheet_id)
    title = spreadsheet.title
    ws_titles = [ws.title for ws in spreadsheet.worksheets()]

    worksheet = spreadsheet.worksheet(worksheet_name)
    all_values = worksheet.get_all_values()

    if not all_values or len(all_values) < 2:
        return title, ws_titles, []

    headers = _make_unique_headers(all_values[0])
    data = []
    for row_values in all_values[1:]:
        row_dict = {}
        for i, header in enumerate(headers):
            row_dict[header] = row_values[i] if i < len(row_values) else ""
        data.append(row_dict)

    return title, ws_titles, data


def get_worksheet(
    client: gspread.Client,
    sheet_id: str,
    worksheet_name: str = "Sheet1",
) -> gspread.Worksheet:
    """워크시트 객체 반환"""
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(worksheet_name)


def find_column_index(worksheet: gspread.Worksheet, column_name: str) -> int:
    """헤더 행에서 컬럼 인덱스(1-based)를 찾습니다."""
    headers = worksheet.row_values(1)
    normalized_target = column_name.replace(" ", "").replace("\n", "").strip()
    for i, header in enumerate(headers, 1):
        if header.replace(" ", "").replace("\n", "").strip() == normalized_target:
            return i
    raise ValueError(f"컬럼을 찾을 수 없습니다: '{column_name}'")


def highlight_rows(
    worksheet: gspread.Worksheet,
    row_numbers: list[int],
    note_col_idx: int,
    notes: list[str],
    end_date_col_idx: Optional[int] = None,
    end_dates: Optional[list[str]] = None,
):
    """정산 시트에서 종료 건 행에 배경색 + 취소선을 적용하고 비고를 기록합니다."""
    if not row_numbers:
        return

    total_cols = len(worksheet.row_values(1))
    sheet_id = worksheet.id
    requests = []

    for i, row_num in enumerate(row_numbers):
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 0,
                    "endColumnIndex": total_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HIGHLIGHT_BG_COLOR,
                        "textFormat": {"strikethrough": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat.strikethrough)",
            }
        })

    if requests:
        worksheet.spreadsheet.batch_update({"requests": requests})

    cells_to_update = []
    for i, row_num in enumerate(row_numbers):
        if i < len(notes):
            cells_to_update.append(
                gspread.Cell(row=row_num, col=note_col_idx, value=notes[i])
            )
        if end_date_col_idx and end_dates and i < len(end_dates) and end_dates[i]:
            cells_to_update.append(
                gspread.Cell(row=row_num, col=end_date_col_idx, value=end_dates[i])
            )

    if cells_to_update:
        worksheet.update_cells(cells_to_update)
