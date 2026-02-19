"""
정산 시트 업데이트 모듈

매칭 결과를 바탕으로 정산 시트의 '계약변경/해지/비고' 컬럼을 업데이트합니다.
dry-run 모드를 지원하여 실제 변경 전에 미리보기가 가능합니다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    ALREADY_TERMINATED_KEYWORDS,
    SETTLEMENT_COLUMNS,
    SYNC_COLUMNS,
    TERMINATION_NOTE_TEMPLATE,
)
from src.matcher import MatchResult, MatchStatus, get_column_value
from src.sheet_reader import SheetReader


class SheetUpdater:
    """정산 시트 업데이트 클래스"""

    def __init__(self, sheet_reader: SheetReader, dry_run: bool = True):
        """
        Args:
            sheet_reader: SheetReader 인스턴스
            dry_run: True이면 실제 업데이트 없이 미리보기만 출력
        """
        self.sheet_reader = sheet_reader
        self.dry_run = dry_run
        self.update_count = 0
        self.skip_count = 0
        self.updates_log: list[dict] = []

    def apply_updates(self, match_results: list[MatchResult]) -> list[dict]:
        """
        매칭 결과를 정산 시트에 반영합니다.

        Args:
            match_results: 매칭 결과 리스트

        Returns:
            업데이트 로그 리스트
        """
        # 정확매칭 또는 검증통과한 유사매칭 건만 업데이트 대상
        updatable = [
            r for r in match_results
            if r.status == MatchStatus.EXACT
            or (r.status == MatchStatus.FUZZY and r.verification_passed)
        ]

        if not updatable:
            print("[INFO] 업데이트할 건이 없습니다.")
            return self.updates_log

        print(f"\n[INFO] 업데이트 대상: {len(updatable)}건 (dry_run={self.dry_run})")
        print(f"{'=' * 70}")

        if not self.dry_run:
            worksheet = self.sheet_reader.get_settlement_worksheet()
            # 비고 컬럼 인덱스 찾기
            note_col_name = SETTLEMENT_COLUMNS["비고"]
            note_col_idx = self.sheet_reader.find_column_index(worksheet, note_col_name)

            # 계약종료일자 컬럼 인덱스 찾기
            end_date_col_name = SETTLEMENT_COLUMNS["계약종료일자"]
            try:
                end_date_col_idx = self.sheet_reader.find_column_index(
                    worksheet, end_date_col_name
                )
            except ValueError:
                end_date_col_idx = None

        cells_to_update = []

        for result in updatable:
            # 종료 시트에서 만기 날짜 가져오기
            expiry_date = get_column_value(
                result.termination_row,
                SYNC_COLUMNS["계약만기날짜"]["termination"],
            )

            # 정산 시트의 기존 비고 값 확인
            existing_note = get_column_value(
                result.settlement_row,
                SETTLEMENT_COLUMNS["비고"],
            )

            # 이미 종료 처리된 건인지 확인
            if self._is_already_terminated(existing_note):
                self.skip_count += 1
                log_entry = {
                    "상태": "건너뜀(이미처리)",
                    "지점명": get_column_value(
                        result.termination_row, "지점명"
                    ),
                    "계약자명": get_column_value(
                        result.termination_row, "계약자명"
                    ),
                    "호실": get_column_value(result.termination_row, "호실"),
                    "기존비고": existing_note,
                    "매칭상세": result.match_details,
                }
                self.updates_log.append(log_entry)
                print(
                    f"  [건너뜀] {log_entry['지점명']} / {log_entry['계약자명']} "
                    f"/ {log_entry['호실']} - 이미 종료 처리됨"
                )
                continue

            # 새 비고 값 생성
            new_note = TERMINATION_NOTE_TEMPLATE.format(
                expiry_date=expiry_date if expiry_date else "미확인"
            )

            # 기존 비고가 있으면 뒤에 추가
            if existing_note and existing_note.strip():
                final_note = f"{existing_note} | {new_note}"
            else:
                final_note = new_note

            # 시트 행 번호 계산 (DataFrame 인덱스 + 헤더행 + 1)
            sheet_row = result.settlement_index + 2  # 0-based → 1-based + 헤더

            log_entry = {
                "상태": "업데이트" if not self.dry_run else "미리보기",
                "시트행": sheet_row,
                "지점명": get_column_value(result.termination_row, "지점명"),
                "계약자명": get_column_value(result.termination_row, "계약자명"),
                "호실": get_column_value(result.termination_row, "호실"),
                "만기일": expiry_date,
                "기존비고": existing_note,
                "신규비고": final_note,
                "매칭유형": result.status.value,
                "신뢰도": f"{result.confidence:.1f}%",
                "매칭상세": result.match_details,
            }
            self.updates_log.append(log_entry)
            self.update_count += 1

            # 미리보기 출력
            status_icon = "📝" if self.dry_run else "✅"
            print(
                f"  {status_icon} [{log_entry['매칭유형']}] "
                f"{log_entry['지점명']} / {log_entry['계약자명']} / {log_entry['호실']}"
            )
            print(f"     만기일: {expiry_date}")
            print(f"     비고: '{existing_note}' → '{final_note}'")
            print(f"     신뢰도: {log_entry['신뢰도']}")
            print()

            if not self.dry_run:
                # 비고 컬럼 업데이트 예약
                cells_to_update.append({
                    "row": sheet_row,
                    "col": note_col_idx,
                    "value": final_note,
                })
                # 계약종료일자 컬럼도 업데이트 (존재하는 경우)
                if end_date_col_idx and expiry_date:
                    cells_to_update.append({
                        "row": sheet_row,
                        "col": end_date_col_idx,
                        "value": expiry_date,
                    })

        # 실제 업데이트 수행
        if not self.dry_run and cells_to_update:
            print(f"\n[INFO] 구글 시트에 {len(cells_to_update)}개 셀 업데이트 중...")
            self.sheet_reader.update_cells(worksheet, cells_to_update)

        print(f"\n{'=' * 70}")
        print(f"[결과] 업데이트: {self.update_count}건 / 건너뜀: {self.skip_count}건")
        if self.dry_run:
            print("[INFO] dry-run 모드입니다. 실제 변경하려면 --dry-run 없이 실행하세요.")

        return self.updates_log

    def _is_already_terminated(self, note: str) -> bool:
        """
        비고 값에 이미 종료 처리 키워드가 있는지 확인합니다.

        Args:
            note: 비고 값

        Returns:
            이미 종료 처리된 경우 True
        """
        if not note or not note.strip():
            return False
        for keyword in ALREADY_TERMINATED_KEYWORDS:
            if keyword in note:
                return True
        return False

