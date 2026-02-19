"""
매칭 엔진 모듈

정산 시트와 계약 종료 시트 간 데이터를 매칭하는 로직입니다.
1차 정확 매칭 → 퍼지 매칭 → 교차 검증 순서로 진행합니다.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd
from rapidfuzz import fuzz

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    DATE_FORMATS,
    FUZZY_THRESHOLD,
    MATCHING_KEYS_PRIMARY,
    MATCHING_KEYS_VERIFICATION,
    NAME_FALLBACK_COLUMNS_SETTLEMENT,
    NAME_FALLBACK_COLUMNS_TERMINATION,
    NORMALIZE_REMOVE_CHARS,
)


class MatchStatus(Enum):
    """매칭 결과 상태"""
    EXACT = "정확매칭"
    FUZZY = "유사매칭(수동확인)"
    UNMATCHED = "매칭실패"


@dataclass
class MatchResult:
    """개별 매칭 결과"""
    termination_index: int  # 종료 시트 행 인덱스
    settlement_index: Optional[int]  # 정산 시트 행 인덱스 (매칭 실패 시 None)
    status: MatchStatus
    confidence: float  # 매칭 신뢰도 (0~100)
    match_details: str  # 매칭 상세 설명
    termination_row: dict = field(default_factory=dict)  # 종료 시트 원본 데이터
    settlement_row: dict = field(default_factory=dict)  # 정산 시트 원본 데이터
    verification_passed: bool = False  # 교차 검증 통과 여부


def normalize_text(text: str) -> str:
    """
    텍스트를 정규화합니다 (공백, 특수문자 제거, 소문자 변환).

    Args:
        text: 원본 텍스트

    Returns:
        정규화된 텍스트
    """
    if pd.isna(text) or text is None:
        return ""
    text = str(text).strip()
    for char in NORMALIZE_REMOVE_CHARS:
        text = text.replace(char, "")
    return text.lower()


def is_likely_name(text: str) -> bool:
    """
    주어진 텍스트가 이름(사람 이름)인지 판별합니다.
    주민등록번호 칸에 이름이 들어가 있는 경우를 감지하기 위한 함수입니다.

    판별 기준:
    - 숫자만으로 이루어져 있으면 이름이 아님 (주민번호)
    - 한글 2~5자면 한국 이름으로 판별
    - 영문 알파벳이 포함되어 있으면 영문 이름으로 판별
    - 숫자+하이픈 패턴이면 주민번호로 판별 (예: 900101-1234567)

    Args:
        text: 검사할 텍스트

    Returns:
        이름으로 판별되면 True
    """
    if pd.isna(text) or text is None:
        return False
    text = str(text).strip()
    if not text:
        return False

    # 숫자와 하이픈만 있으면 주민번호 → 이름 아님
    if re.match(r"^[\d\-\s]+$", text):
        return False

    # 한글 이름 패턴 (2~5자 한글, 공백 포함 가능)
    korean_name = re.sub(r"\s+", "", text)
    if re.match(r"^[가-힣]{2,5}$", korean_name):
        return True

    # 영문 이름 패턴 (알파벳 + 공백으로 구성, 최소 2글자)
    if re.match(r"^[a-zA-Z\s\.\-]{2,}$", text) and any(c.isalpha() for c in text):
        return True

    # 한글+영문 혼합 이름 (예: "김John")
    if any('\uAC00' <= c <= '\uD7A3' for c in text) and any(c.isalpha() and ord(c) < 128 for c in text):
        return True

    return False


def normalize_name(name: str) -> str:
    """
    이름을 정규화합니다. 한글명과 영문명 모두 처리합니다.

    처리 내용:
    - 앞뒤 공백 제거
    - 연속 공백 → 단일 공백
    - 영문: 소문자 변환, 이름 사이 공백 통일
    - 한글: 공백 제거 (예: "홍 길동" → "홍길동")

    Args:
        name: 원본 이름

    Returns:
        정규화된 이름
    """
    if pd.isna(name) or name is None:
        return ""
    name = str(name).strip()
    if not name:
        return ""

    # 연속 공백 정리
    name = re.sub(r"\s+", " ", name)

    # 한글만 있는 경우 → 공백 제거 후 반환
    if re.match(r"^[가-힣\s]+$", name):
        return name.replace(" ", "")

    # 영문만 있는 경우 → 소문자 변환, 공백 유지
    if re.match(r"^[a-zA-Z\s\.\-]+$", name):
        # 점(.) 뒤 공백 통일, 소문자 변환
        name = name.replace(".", " ").replace("-", " ")
        name = re.sub(r"\s+", " ", name).strip()
        return name.lower()

    # 한글+영문 혼합 → 소문자 변환, 공백 제거
    return name.replace(" ", "").lower()


def extract_contractor_name(row: dict, name_column: str, fallback_columns: list[str]) -> str:
    """
    계약자명을 추출합니다. 계약자명이 비어있으면 폴백 컬럼에서 이름을 찾습니다.

    예시:
    - 계약자명 = "홍길동" → "홍길동" 반환
    - 계약자명 = "", 주민번호 = "홍길동" → "홍길동" 반환 (폴백)
    - 계약자명 = "", 주민번호 = "900101-1234567" → "" 반환 (주민번호는 이름이 아님)
    - 계약자명 = "John Smith" → "John Smith" 반환

    Args:
        row: 행 데이터 (dict)
        name_column: 계약자명 컬럼명
        fallback_columns: 폴백 컬럼명 리스트 (순서대로 확인)

    Returns:
        추출된 계약자명
    """
    # 1차: 원래 계약자명 컬럼에서 가져오기
    name = get_column_value(row, name_column)
    if name.strip():
        return name.strip()

    # 2차: 폴백 컬럼에서 이름 찾기
    for fallback_col in fallback_columns:
        fallback_val = get_column_value(row, fallback_col)
        if fallback_val.strip() and is_likely_name(fallback_val):
            return fallback_val.strip()

    return ""


def normalize_business_number(biz_num: str) -> str:
    """
    사업자등록번호를 정규화합니다 (숫자만 추출).

    Args:
        biz_num: 사업자등록번호 (예: "123-45-67890" → "1234567890")

    Returns:
        숫자만 남긴 사업자등록번호
    """
    if pd.isna(biz_num) or biz_num is None:
        return ""
    return re.sub(r"[^0-9]", "", str(biz_num))


def parse_date(date_str: str) -> Optional[datetime]:
    """
    다양한 형식의 날짜 문자열을 파싱합니다.

    Args:
        date_str: 날짜 문자열

    Returns:
        datetime 객체 또는 None (파싱 실패 시)
    """
    if pd.isna(date_str) or date_str is None or str(date_str).strip() == "":
        return None
    date_str = str(date_str).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def get_column_value(row: dict, column_name: str) -> str:
    """
    행(dict)에서 컬럼 값을 안전하게 가져옵니다.
    컬럼명에 공백/줄바꿈이 있을 수 있으므로 유연하게 매칭합니다.

    Args:
        row: 행 데이터 (dict)
        column_name: 컬럼명

    Returns:
        컬럼 값 (문자열)
    """
    # 정확한 키로 먼저 시도
    if column_name in row:
        return str(row[column_name]) if not pd.isna(row.get(column_name)) else ""

    # 공백/줄바꿈 제거 후 매칭 시도
    normalized_target = column_name.replace(" ", "").replace("\n", "").strip()
    for key in row.keys():
        normalized_key = str(key).replace(" ", "").replace("\n", "").strip()
        if normalized_key == normalized_target:
            val = row[key]
            return str(val) if not pd.isna(val) else ""

    return ""


class ContractMatcher:
    """계약 매칭 엔진"""

    def __init__(
        self,
        settlement_df: pd.DataFrame,
        termination_df: pd.DataFrame,
        fuzzy_threshold: int = FUZZY_THRESHOLD,
    ):
        """
        Args:
            settlement_df: 정산 시트 DataFrame
            termination_df: 계약 종료 시트 DataFrame
            fuzzy_threshold: 퍼지 매칭 임계값 (0~100)
        """
        self.settlement_df = settlement_df
        self.termination_df = termination_df
        self.fuzzy_threshold = fuzzy_threshold
        self.results: list[MatchResult] = []
        # 이미 매칭된 정산 시트 인덱스 (중복 매칭 방지)
        self._matched_settlement_indices: set[int] = set()

    def run(self) -> list[MatchResult]:
        """
        전체 매칭 프로세스를 실행합니다.

        Returns:
            매칭 결과 리스트
        """
        print(f"\n[INFO] 매칭 시작: 종료 시트 {len(self.termination_df)}건 → 정산 시트 {len(self.settlement_df)}건")
        print(f"[INFO] 퍼지 매칭 임계값: {self.fuzzy_threshold}%")

        self.results = []
        self._matched_settlement_indices = set()

        for t_idx, t_row in self.termination_df.iterrows():
            t_row_dict = t_row.to_dict()

            # 1단계: 정확 매칭 시도
            result = self._exact_match(t_idx, t_row_dict)
            if result is None:
                # 2단계: 퍼지 매칭 시도
                result = self._fuzzy_match(t_idx, t_row_dict)

            if result is None:
                # 매칭 실패
                result = MatchResult(
                    termination_index=t_idx,
                    settlement_index=None,
                    status=MatchStatus.UNMATCHED,
                    confidence=0.0,
                    match_details="매칭되는 정산 건을 찾을 수 없음",
                    termination_row=t_row_dict,
                )

            # 3단계: 교차 검증 (매칭 성공 건에 한해)
            if result.settlement_index is not None:
                result = self._cross_verify(result)
                # 중복 매칭 방지: 매칭된 정산 인덱스 기록
                self._matched_settlement_indices.add(result.settlement_index)

            self.results.append(result)

        self._print_summary()
        return self.results

    def _extract_names(self, t_row: dict, s_row_dict: dict) -> tuple[str, str, str, str]:
        """
        종료 시트와 정산 시트에서 계약자명을 추출합니다.
        폴백 로직과 이름 정규화를 적용합니다.

        Args:
            t_row: 종료 시트 행 데이터
            s_row_dict: 정산 시트 행 데이터

        Returns:
            (종료_원본이름, 정산_원본이름, 종료_정규화이름, 정산_정규화이름)
        """
        # 종료 시트: 계약자명 → 주민번호(이름 폴백) → 상호 순서로 추출
        t_name_raw = extract_contractor_name(
            t_row,
            MATCHING_KEYS_PRIMARY["계약자명"]["termination"],
            NAME_FALLBACK_COLUMNS_TERMINATION,
        )
        # 정산 시트: 계약자명 → 폴백 순서로 추출
        s_name_raw = extract_contractor_name(
            s_row_dict,
            MATCHING_KEYS_PRIMARY["계약자명"]["settlement"],
            NAME_FALLBACK_COLUMNS_SETTLEMENT,
        )
        t_name_norm = normalize_name(t_name_raw)
        s_name_norm = normalize_name(s_name_raw)

        return t_name_raw, s_name_raw, t_name_norm, s_name_norm

    def _exact_match(
        self, t_idx: int, t_row: dict
    ) -> Optional[MatchResult]:
        """
        정확 매칭: 지점명 + 계약자명 + 호실이 정확히 일치하는 건을 찾습니다.
        계약자명이 비어있을 때 주민번호 칸의 이름을 폴백으로 사용합니다.
        영문 이름은 대소문자/공백 정규화 후 비교합니다.

        Args:
            t_idx: 종료 시트 행 인덱스
            t_row: 종료 시트 행 데이터

        Returns:
            MatchResult 또는 None
        """
        t_branch = normalize_text(
            get_column_value(t_row, MATCHING_KEYS_PRIMARY["지점명"]["termination"])
        )
        t_room = normalize_text(
            get_column_value(t_row, MATCHING_KEYS_PRIMARY["호실"]["termination"])
        )

        # 계약자명 추출 (폴백 포함)
        t_name_raw = extract_contractor_name(
            t_row,
            MATCHING_KEYS_PRIMARY["계약자명"]["termination"],
            NAME_FALLBACK_COLUMNS_TERMINATION,
        )
        t_contractor = normalize_name(t_name_raw)

        if not t_branch and not t_contractor:
            return None

        # 폴백 사용 여부 기록
        original_name = get_column_value(t_row, MATCHING_KEYS_PRIMARY["계약자명"]["termination"])
        used_fallback = (not original_name.strip()) and t_name_raw.strip()

        for s_idx, s_row in self.settlement_df.iterrows():
            # 중복 매칭 방지: 이미 매칭된 정산 건 건너뛰기
            if s_idx in self._matched_settlement_indices:
                continue

            s_row_dict = s_row.to_dict()

            s_branch = normalize_text(
                get_column_value(s_row_dict, MATCHING_KEYS_PRIMARY["지점명"]["settlement"])
            )
            s_name_raw = extract_contractor_name(
                s_row_dict,
                MATCHING_KEYS_PRIMARY["계약자명"]["settlement"],
                NAME_FALLBACK_COLUMNS_SETTLEMENT,
            )
            s_contractor = normalize_name(s_name_raw)
            s_room = normalize_text(
                get_column_value(s_row_dict, MATCHING_KEYS_PRIMARY["호실"]["settlement"])
            )

            # 3가지 모두 일치
            if t_branch == s_branch and t_contractor == s_contractor and t_room == s_room:
                fallback_note = " (주민번호칸에서 이름 추출)" if used_fallback else ""
                return MatchResult(
                    termination_index=t_idx,
                    settlement_index=s_idx,
                    status=MatchStatus.EXACT,
                    confidence=100.0,
                    match_details=(
                        f"정확매칭: 지점명={t_branch}, "
                        f"계약자명={t_name_raw}{fallback_note}, 호실={t_room}"
                    ),
                    termination_row=t_row,
                    settlement_row=s_row_dict,
                )

            # 지점명 + 계약자명만 일치 (호실이 비어있는 경우)
            if t_branch == s_branch and t_contractor == s_contractor:
                if not t_room or not s_room:
                    fallback_note = " (주민번호칸에서 이름 추출)" if used_fallback else ""
                    return MatchResult(
                        termination_index=t_idx,
                        settlement_index=s_idx,
                        status=MatchStatus.EXACT,
                        confidence=90.0,
                        match_details=(
                            f"정확매칭(호실미확인): 지점명={t_branch}, "
                            f"계약자명={t_name_raw}{fallback_note}"
                        ),
                        termination_row=t_row,
                        settlement_row=s_row_dict,
                    )

        return None

    def _fuzzy_match(
        self, t_idx: int, t_row: dict
    ) -> Optional[MatchResult]:
        """
        퍼지 매칭: 유사도 기반으로 매칭합니다.
        영문 이름, 한글 이름 모두 정규화 후 유사도를 계산합니다.
        계약자명이 비어있으면 주민번호 칸에서 이름을 폴백 추출합니다.

        Args:
            t_idx: 종료 시트 행 인덱스
            t_row: 종료 시트 행 데이터

        Returns:
            MatchResult 또는 None
        """
        t_branch = get_column_value(
            t_row, MATCHING_KEYS_PRIMARY["지점명"]["termination"]
        )
        # 계약자명 추출 (폴백 포함)
        t_contractor = extract_contractor_name(
            t_row,
            MATCHING_KEYS_PRIMARY["계약자명"]["termination"],
            NAME_FALLBACK_COLUMNS_TERMINATION,
        )
        t_room = get_column_value(
            t_row, MATCHING_KEYS_PRIMARY["호실"]["termination"]
        )

        if not t_branch.strip() and not t_contractor.strip():
            return None

        # 폴백 사용 여부
        original_name = get_column_value(t_row, MATCHING_KEYS_PRIMARY["계약자명"]["termination"])
        used_fallback = (not original_name.strip()) and t_contractor.strip()

        best_score = 0.0
        best_s_idx = None
        best_s_row = None
        best_detail = ""

        for s_idx, s_row in self.settlement_df.iterrows():
            # 중복 매칭 방지: 이미 매칭된 정산 건 건너뛰기
            if s_idx in self._matched_settlement_indices:
                continue

            s_row_dict = s_row.to_dict()

            s_branch = get_column_value(
                s_row_dict, MATCHING_KEYS_PRIMARY["지점명"]["settlement"]
            )
            s_contractor = extract_contractor_name(
                s_row_dict,
                MATCHING_KEYS_PRIMARY["계약자명"]["settlement"],
                NAME_FALLBACK_COLUMNS_SETTLEMENT,
            )
            s_room = get_column_value(
                s_row_dict, MATCHING_KEYS_PRIMARY["호실"]["settlement"]
            )

            # 각 필드의 유사도 계산
            branch_score = fuzz.ratio(
                normalize_text(t_branch), normalize_text(s_branch)
            ) if t_branch.strip() and s_branch.strip() else 0

            # 계약자명: normalize_name 사용 (영문명 대소문자/공백 정규화)
            t_name_norm = normalize_name(t_contractor)
            s_name_norm = normalize_name(s_contractor)
            if t_name_norm and s_name_norm:
                # fuzz.ratio와 fuzz.token_sort_ratio 중 높은 값 사용
                # token_sort_ratio: 단어 순서 무관 비교 (예: "John Smith" vs "Smith John")
                contractor_score = max(
                    fuzz.ratio(t_name_norm, s_name_norm),
                    fuzz.token_sort_ratio(t_name_norm, s_name_norm),
                )
            else:
                contractor_score = 0

            room_score = fuzz.ratio(
                normalize_text(t_room), normalize_text(s_room)
            ) if t_room.strip() and s_room.strip() else 100  # 호실이 비어있으면 일치로 간주

            # 가중 평균 (지점명 30%, 계약자명 50%, 호실 20%)
            total_score = branch_score * 0.3 + contractor_score * 0.5 + room_score * 0.2

            if total_score > best_score:
                best_score = total_score
                best_s_idx = s_idx
                best_s_row = s_row_dict
                fallback_note = " [이름폴백]" if used_fallback else ""
                best_detail = (
                    f"유사매칭: 지점={branch_score:.0f}%, "
                    f"계약자={contractor_score:.0f}%{fallback_note}, "
                    f"호실={room_score:.0f}% → 종합={total_score:.1f}%"
                )

        if best_score >= self.fuzzy_threshold and best_s_idx is not None:
            return MatchResult(
                termination_index=t_idx,
                settlement_index=best_s_idx,
                status=MatchStatus.FUZZY,
                confidence=best_score,
                match_details=best_detail,
                termination_row=t_row,
                settlement_row=best_s_row,
            )

        return None

    def _cross_verify(self, result: MatchResult) -> MatchResult:
        """
        교차 검증: 사업자등록번호 또는 계약시작일자로 매칭을 확인합니다.

        Args:
            result: 매칭 결과

        Returns:
            검증 결과가 추가된 MatchResult
        """
        verified_items = []

        # 사업자등록번호 검증
        t_biz = normalize_business_number(
            get_column_value(
                result.termination_row,
                MATCHING_KEYS_VERIFICATION["사업자등록번호"]["termination"],
            )
        )
        s_biz = normalize_business_number(
            get_column_value(
                result.settlement_row,
                MATCHING_KEYS_VERIFICATION["사업자등록번호"]["settlement"],
            )
        )
        if t_biz and s_biz:
            if t_biz == s_biz:
                verified_items.append("사업자번호일치")
            else:
                verified_items.append("사업자번호불일치")

        # 계약시작일자 검증
        t_date = parse_date(
            get_column_value(
                result.termination_row,
                MATCHING_KEYS_VERIFICATION["계약시작일자"]["termination"],
            )
        )
        s_date = parse_date(
            get_column_value(
                result.settlement_row,
                MATCHING_KEYS_VERIFICATION["계약시작일자"]["settlement"],
            )
        )
        if t_date and s_date:
            if t_date == s_date:
                verified_items.append("계약시작일일치")
            else:
                verified_items.append("계약시작일불일치")

        # 검증 결과 반영
        has_match = any("일치" in v and "불일치" not in v for v in verified_items)
        has_mismatch = any("불일치" in v for v in verified_items)

        if has_match and not has_mismatch:
            result.verification_passed = True
            result.match_details += f" [검증통과: {', '.join(verified_items)}]"
        elif has_mismatch:
            result.verification_passed = False
            result.match_details += f" [검증실패: {', '.join(verified_items)}]"
            # 정확 매칭이었지만 교차 검증 실패 → 수동확인으로 변경
            if result.status == MatchStatus.EXACT:
                result.status = MatchStatus.FUZZY
                result.match_details += " → 수동확인 필요"
        else:
            # 검증 데이터 없음 (빈 값)
            result.verification_passed = True  # 데이터 없으면 통과로 간주
            result.match_details += " [검증데이터 없음]"

        return result

    def _print_summary(self):
        """매칭 결과 요약 출력"""
        total = len(self.results)
        exact = sum(1 for r in self.results if r.status == MatchStatus.EXACT)
        fuzzy = sum(1 for r in self.results if r.status == MatchStatus.FUZZY)
        unmatched = sum(1 for r in self.results if r.status == MatchStatus.UNMATCHED)

        print(f"\n{'=' * 60}")
        print(f"[매칭 결과 요약]")
        print(f"  총 {total}건 중:")
        print(f"  ✓ 정확매칭: {exact}건")
        print(f"  △ 유사매칭(수동확인): {fuzzy}건")
        print(f"  ✗ 매칭실패: {unmatched}건")
        print(f"{'=' * 60}\n")

