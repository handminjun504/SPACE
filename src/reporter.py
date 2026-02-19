"""
결과 리포트 생성 모듈

매칭 결과를 콘솔에 출력하고 CSV 파일로 저장합니다.
"""

import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    MATCHING_KEYS_PRIMARY,
    MATCHING_KEYS_VERIFICATION,
    OUTPUT_DIR,
    REPORT_FILENAME_TEMPLATE,
    SYNC_COLUMNS,
)
from src.matcher import MatchResult, MatchStatus, get_column_value


class Reporter:
    """매칭 결과 리포트 생성 클래스"""

    def __init__(self, match_results: list[MatchResult], updates_log: list[dict]):
        """
        Args:
            match_results: 매칭 엔진의 결과 리스트
            updates_log: 업데이터의 로그 리스트
        """
        self.match_results = match_results
        self.updates_log = updates_log

    def print_detailed_report(self):
        """상세 리포트를 콘솔에 출력합니다."""
        print("\n")
        print("=" * 80)
        print("          스페이스액스키 계약 종료 동기화 - 상세 리포트")
        print("=" * 80)
        print(f"  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        total = len(self.match_results)
        exact = [r for r in self.match_results if r.status == MatchStatus.EXACT]
        fuzzy = [r for r in self.match_results if r.status == MatchStatus.FUZZY]
        unmatched = [r for r in self.match_results if r.status == MatchStatus.UNMATCHED]

        print(f"\n  [총계] {total}건")
        print(f"  ├─ 정확매칭: {len(exact)}건")
        print(f"  ├─ 유사매칭(수동확인): {len(fuzzy)}건")
        print(f"  └─ 매칭실패: {len(unmatched)}건")

        # 정확 매칭 건 상세
        if exact:
            print(f"\n{'─' * 80}")
            print(f"  ✓ 정확매칭 ({len(exact)}건)")
            print(f"{'─' * 80}")
            for i, r in enumerate(exact, 1):
                self._print_match_detail(i, r)

        # 유사 매칭 건 상세
        if fuzzy:
            print(f"\n{'─' * 80}")
            print(f"  △ 유사매칭 - 수동 확인 필요 ({len(fuzzy)}건)")
            print(f"{'─' * 80}")
            for i, r in enumerate(fuzzy, 1):
                self._print_match_detail(i, r, show_comparison=True)

        # 매칭 실패 건 상세
        if unmatched:
            print(f"\n{'─' * 80}")
            print(f"  ✗ 매칭실패 ({len(unmatched)}건)")
            print(f"{'─' * 80}")
            for i, r in enumerate(unmatched, 1):
                t_branch = get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_PRIMARY["지점명"]["termination"],
                )
                t_contractor = get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_PRIMARY["계약자명"]["termination"],
                )
                t_room = get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_PRIMARY["호실"]["termination"],
                )
                t_expiry = get_column_value(
                    r.termination_row,
                    SYNC_COLUMNS["계약만기날짜"]["termination"],
                )
                print(f"  {i}. {t_branch} / {t_contractor} / 호실: {t_room} / 만기: {t_expiry}")

        print(f"\n{'=' * 80}\n")

    def _print_match_detail(
        self, idx: int, result: MatchResult, show_comparison: bool = False
    ):
        """
        개별 매칭 결과를 출력합니다.

        Args:
            idx: 순번
            result: 매칭 결과
            show_comparison: 비교 정보를 보여줄지 여부
        """
        t_branch = get_column_value(
            result.termination_row,
            MATCHING_KEYS_PRIMARY["지점명"]["termination"],
        )
        t_contractor = get_column_value(
            result.termination_row,
            MATCHING_KEYS_PRIMARY["계약자명"]["termination"],
        )
        t_room = get_column_value(
            result.termination_row,
            MATCHING_KEYS_PRIMARY["호실"]["termination"],
        )
        t_expiry = get_column_value(
            result.termination_row,
            SYNC_COLUMNS["계약만기날짜"]["termination"],
        )

        print(
            f"  {idx}. {t_branch} / {t_contractor} / 호실: {t_room} "
            f"/ 만기: {t_expiry} [신뢰도: {result.confidence:.1f}%]"
        )

        if show_comparison and result.settlement_row:
            s_branch = get_column_value(
                result.settlement_row,
                MATCHING_KEYS_PRIMARY["지점명"]["settlement"],
            )
            s_contractor = get_column_value(
                result.settlement_row,
                MATCHING_KEYS_PRIMARY["계약자명"]["settlement"],
            )
            s_room = get_column_value(
                result.settlement_row,
                MATCHING_KEYS_PRIMARY["호실"]["settlement"],
            )
            print(
                f"     → 정산시트: {s_branch} / {s_contractor} / 호실: {s_room}"
            )
            print(f"     → 상세: {result.match_details}")

    def save_csv_report(self) -> str:
        """
        매칭 결과를 CSV 파일로 저장합니다.

        Returns:
            저장된 파일 경로
        """
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = REPORT_FILENAME_TEMPLATE.format(timestamp=timestamp)
        filepath = os.path.join(OUTPUT_DIR, filename)

        rows = []
        for r in self.match_results:
            row = {
                "매칭상태": r.status.value,
                "신뢰도(%)": round(r.confidence, 1),
                "검증통과": "Y" if r.verification_passed else "N",
                "종료_지점명": get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_PRIMARY["지점명"]["termination"],
                ),
                "종료_계약자명": get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_PRIMARY["계약자명"]["termination"],
                ),
                "종료_호실": get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_PRIMARY["호실"]["termination"],
                ),
                "종료_만기날짜": get_column_value(
                    r.termination_row,
                    SYNC_COLUMNS["계약만기날짜"]["termination"],
                ),
                "종료_사업자번호": get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_VERIFICATION["사업자등록번호"]["termination"],
                ),
                "종료_계약시작일": get_column_value(
                    r.termination_row,
                    MATCHING_KEYS_VERIFICATION["계약시작일자"]["termination"],
                ),
            }

            if r.settlement_row:
                row.update({
                    "정산_지점명": get_column_value(
                        r.settlement_row,
                        MATCHING_KEYS_PRIMARY["지점명"]["settlement"],
                    ),
                    "정산_계약자명": get_column_value(
                        r.settlement_row,
                        MATCHING_KEYS_PRIMARY["계약자명"]["settlement"],
                    ),
                    "정산_호실": get_column_value(
                        r.settlement_row,
                        MATCHING_KEYS_PRIMARY["호실"]["settlement"],
                    ),
                    "정산_사업자번호": get_column_value(
                        r.settlement_row,
                        MATCHING_KEYS_VERIFICATION["사업자등록번호"]["settlement"],
                    ),
                    "정산_계약시작일": get_column_value(
                        r.settlement_row,
                        MATCHING_KEYS_VERIFICATION["계약시작일자"]["settlement"],
                    ),
                })
            else:
                row.update({
                    "정산_지점명": "",
                    "정산_계약자명": "",
                    "정산_호실": "",
                    "정산_사업자번호": "",
                    "정산_계약시작일": "",
                })

            row["매칭상세"] = r.match_details
            rows.append(row)

        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        print(f"[INFO] CSV 리포트 저장: {filepath}")
        return filepath

    def save_updates_log(self) -> str:
        """
        업데이트 로그를 CSV 파일로 저장합니다.

        Returns:
            저장된 파일 경로
        """
        if not self.updates_log:
            print("[INFO] 업데이트 로그가 비어 있습니다.")
            return ""

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"update_log_{timestamp}.csv"
        filepath = os.path.join(OUTPUT_DIR, filename)

        df = pd.DataFrame(self.updates_log)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        print(f"[INFO] 업데이트 로그 저장: {filepath}")
        return filepath

