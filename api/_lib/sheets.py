"""
Google Sheets 읽기/쓰기/포맷팅 모듈 (Vercel 서버리스용)

서비스 계정 JSON은 환경변수 GOOGLE_CREDENTIALS_JSON에서 로드합니다.
"""

import json
import os
import re
from typing import Optional

import gspread
import pandas as pd
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
    """
    구글 시트 URL 또는 ID에서 시트 ID를 추출합니다.

    Args:
        url_or_id: 구글 시트 URL 또는 시트 ID

    Returns:
        시트 ID 문자열
    """
    url_or_id = url_or_id.strip()
    # URL 패턴: https://docs.google.com/spreadsheets/d/SHEET_ID/...
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # 이미 ID 형태인 경우
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


def read_sheet_as_dataframe(
    client: gspread.Client,
    sheet_id: str,
    worksheet_name: str = "Sheet1",
) -> pd.DataFrame:
    """
    구글 시트를 DataFrame으로 읽어옵니다.
    헤더 중복이 있어도 안전하게 처리합니다.
    """
    try:
        spreadsheet = client.open_by_key(sheet_id)
    except Exception as e:
        raise PermissionError(
            f"시트(ID: {sheet_id[:12]}...) 열기 실패. "
            f"서비스 계정에 시트 공유가 필요합니다. "
            f"원본 에러: {type(e).__name__}: {str(e)}"
        ) from e

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        available = [ws.title for ws in spreadsheet.worksheets()]
        raise ValueError(
            f"'{worksheet_name}' 탭을 찾을 수 없습니다. "
            f"사용 가능한 탭: {', '.join(available)}"
        )

    # get_all_values()는 헤더 중복 문제 없이 모든 셀 값을 가져옴
    all_values = worksheet.get_all_values()
    if not all_values or len(all_values) < 2:
        return pd.DataFrame()

    headers = _make_unique_headers(all_values[0])
    data = all_values[1:]

    return pd.DataFrame(data, columns=headers)


def get_worksheet(
    client: gspread.Client,
    sheet_id: str,
    worksheet_name: str = "Sheet1",
) -> gspread.Worksheet:
    """워크시트 객체 반환"""
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(worksheet_name)


def find_column_index(worksheet: gspread.Worksheet, column_name: str) -> int:
    """
    헤더 행에서 컬럼 인덱스(1-based)를 찾습니다.
    공백/줄바꿈 차이를 무시하고 매칭합니다.
    """
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
    """
    정산 시트에서 종료 건 행에 배경색 + 취소선을 적용하고 비고를 기록합니다.

    Args:
        worksheet: 워크시트 객체
        row_numbers: 강조할 행 번호 리스트 (1-based)
        note_col_idx: 비고 컬럼 인덱스 (1-based)
        notes: 각 행에 기록할 비고 텍스트 리스트
        end_date_col_idx: 계약종료일자 컬럼 인덱스 (1-based, 선택)
        end_dates: 각 행의 만기일 리스트 (선택)
    """
    if not row_numbers:
        return

    # 시트의 전체 컬럼 수 확인
    total_cols = len(worksheet.row_values(1))

    # 배치 업데이트용 요청 목록
    spreadsheet_id = worksheet.spreadsheet.id
    sheet_id = worksheet.id

    requests = []

    for i, row_num in enumerate(row_numbers):
        # 1) 배경색 + 취소선 포맷 적용 (전체 행)
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

    # 포맷 일괄 적용
    if requests:
        worksheet.spreadsheet.batch_update({"requests": requests})

    # 2) 비고 컬럼에 텍스트 기록 (batch update)
    cells_to_update = []
    for i, row_num in enumerate(row_numbers):
        if i < len(notes):
            cells_to_update.append(
                gspread.Cell(row=row_num, col=note_col_idx, value=notes[i])
            )
        # 계약종료일자도 업데이트
        if end_date_col_idx and end_dates and i < len(end_dates) and end_dates[i]:
            cells_to_update.append(
                gspread.Cell(row=row_num, col=end_date_col_idx, value=end_dates[i])
            )

    if cells_to_update:
        worksheet.update_cells(cells_to_update)


def get_sheet_title(client: gspread.Client, sheet_id: str) -> str:
    """시트 제목을 가져옵니다. 에러 시 그대로 raise."""
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.title


def list_worksheets(client: gspread.Client, sheet_id: str) -> list[str]:
    """시트의 워크시트(탭) 목록을 가져옵니다."""
    try:
        spreadsheet = client.open_by_key(sheet_id)
        return [ws.title for ws in spreadsheet.worksheets()]
    except Exception:
        return []

