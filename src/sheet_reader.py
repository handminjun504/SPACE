"""
Google Sheets API 연동 모듈

gspread + google-auth를 사용하여 구글 시트를 읽고 쓰는 기능을 제공합니다.
서비스 계정(Service Account) 인증 방식을 사용합니다.
"""

import os
import sys
from typing import Optional

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    GOOGLE_CREDENTIALS_PATH,
    SETTLEMENT_SHEET_ID,
    SETTLEMENT_WORKSHEET_NAME,
    TERMINATION_SHEET_ID,
    TERMINATION_WORKSHEET_NAME,
)

# Google Sheets API 스코프
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class SheetReader:
    """Google Sheets 읽기/쓰기 클래스"""

    def __init__(self, credentials_path: Optional[str] = None):
        """
        Args:
            credentials_path: 서비스 계정 JSON 키 파일 경로.
                             None이면 settings에서 가져옵니다.
        """
        self.credentials_path = credentials_path or GOOGLE_CREDENTIALS_PATH
        self.client: Optional[gspread.Client] = None
        self._connect()

    def _connect(self):
        """Google Sheets API에 인증 및 연결"""
        if not os.path.exists(self.credentials_path):
            raise FileNotFoundError(
                f"서비스 계정 키 파일을 찾을 수 없습니다: {self.credentials_path}\n"
                "1. Google Cloud Console에서 서비스 계정을 생성하세요.\n"
                "2. JSON 키를 다운로드하여 credentials/ 폴더에 저장하세요.\n"
                "3. .env 파일의 GOOGLE_CREDENTIALS_PATH를 확인하세요."
            )
        credentials = Credentials.from_service_account_file(
            self.credentials_path, scopes=SCOPES
        )
        self.client = gspread.authorize(credentials)
        print("[INFO] Google Sheets API 연결 성공")

    def read_sheet_as_dataframe(
        self, sheet_id: str, worksheet_name: str
    ) -> pd.DataFrame:
        """
        구글 시트를 읽어서 DataFrame으로 반환합니다.

        Args:
            sheet_id: 구글 시트 ID (URL에서 /d/ 뒤의 값)
            worksheet_name: 워크시트(탭) 이름

        Returns:
            pd.DataFrame: 시트 데이터
        """
        try:
            spreadsheet = self.client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet(worksheet_name)
            data = worksheet.get_all_records()
            df = pd.DataFrame(data)
            print(f"[INFO] 시트 읽기 완료: {worksheet_name} ({len(df)}행)")
            return df
        except gspread.exceptions.SpreadsheetNotFound:
            raise ValueError(
                f"시트를 찾을 수 없습니다 (ID: {sheet_id}).\n"
                "서비스 계정 이메일에 시트 공유 권한을 부여했는지 확인하세요."
            )
        except gspread.exceptions.WorksheetNotFound:
            raise ValueError(
                f"워크시트를 찾을 수 없습니다: '{worksheet_name}'\n"
                "시트의 탭 이름을 확인하고 .env 파일을 수정하세요."
            )
        except Exception as e:
            raise RuntimeError(f"시트 읽기 실패: {e}")

    def read_settlement_sheet(self) -> pd.DataFrame:
        """정산 시트를 읽어서 DataFrame으로 반환"""
        print("[INFO] 정산 시트 읽는 중...")
        return self.read_sheet_as_dataframe(
            SETTLEMENT_SHEET_ID, SETTLEMENT_WORKSHEET_NAME
        )

    def read_termination_sheet(self) -> pd.DataFrame:
        """계약 종료 시트를 읽어서 DataFrame으로 반환"""
        print("[INFO] 계약 종료 시트 읽는 중...")
        return self.read_sheet_as_dataframe(
            TERMINATION_SHEET_ID, TERMINATION_WORKSHEET_NAME
        )

    def get_worksheet(
        self, sheet_id: str, worksheet_name: str
    ) -> gspread.Worksheet:
        """
        워크시트 객체를 직접 반환합니다 (업데이트 용도).

        Args:
            sheet_id: 구글 시트 ID
            worksheet_name: 워크시트(탭) 이름

        Returns:
            gspread.Worksheet: 워크시트 객체
        """
        spreadsheet = self.client.open_by_key(sheet_id)
        return spreadsheet.worksheet(worksheet_name)

    def get_settlement_worksheet(self) -> gspread.Worksheet:
        """정산 시트의 워크시트 객체 반환"""
        return self.get_worksheet(SETTLEMENT_SHEET_ID, SETTLEMENT_WORKSHEET_NAME)

    def update_cells(
        self, worksheet: gspread.Worksheet, updates: list[dict]
    ):
        """
        여러 셀을 일괄 업데이트합니다.

        Args:
            worksheet: 워크시트 객체
            updates: [{"row": 행번호(1-based), "col": 열번호(1-based), "value": 값}, ...]
        """
        if not updates:
            print("[INFO] 업데이트할 셀이 없습니다.")
            return

        # gspread batch update 사용 (API 호출 최소화)
        cells_to_update = []
        for u in updates:
            cell = worksheet.cell(u["row"], u["col"])
            cell.value = u["value"]
            cells_to_update.append(cell)

        worksheet.update_cells(cells_to_update)
        print(f"[INFO] {len(cells_to_update)}개 셀 업데이트 완료")

    def find_column_index(
        self, worksheet: gspread.Worksheet, column_name: str
    ) -> int:
        """
        헤더 행에서 특정 컬럼의 인덱스(1-based)를 찾습니다.

        Args:
            worksheet: 워크시트 객체
            column_name: 컬럼명

        Returns:
            int: 컬럼 인덱스 (1-based)
        """
        headers = worksheet.row_values(1)
        # 정확한 매칭 시도
        for i, header in enumerate(headers, 1):
            if header.strip() == column_name.strip():
                return i
        # 공백 제거 후 매칭 시도
        normalized_name = column_name.replace(" ", "").replace("\n", "")
        for i, header in enumerate(headers, 1):
            if header.replace(" ", "").replace("\n", "").strip() == normalized_name:
                return i
        raise ValueError(f"컬럼을 찾을 수 없습니다: '{column_name}' (헤더: {headers})")

