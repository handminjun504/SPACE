"""
매칭 엔진 모듈 (Vercel 서버리스용) — v5 (날짜 기준 매칭)

매칭 전략:
  Phase 1 (정확): 지점 + 이름 + 호실 완전 일치
  Phase 2 (날짜): 지점 + 계약시작일 + 계약만기일 완전 일치 → 이름 유사도 확인
  Phase 3 (퍼지): 지점 내 이름+호실+날짜 종합 유사도

대규모 데이터 대응 (정산 12,831행 × 종료 8,329행):
- 사전 인덱스 + O(1) 룩업
- rapidfuzz.process.extractOne (C 확장)
- 시간 제한: 15초 (안전 마진)
"""

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from rapidfuzz import fuzz, process as rfprocess

# ============================================================
# 설정 상수
# ============================================================
FUZZY_THRESHOLD = 80          # 종합 매칭 임계값 (%)
FUZZY_TIME_LIMIT = 15         # 퍼지 매칭 최대 시간 (초)
DATE_MATCH_NAME_THRESHOLD = 40  # 날짜 매칭 시 이름 유사도 최소값

NORMALIZE_REMOVE_CHARS = [" ", "\t", "\n", "-", ".", "(", ")", "\u3000"]
DATE_FORMATS = [
    "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d",
    "%y-%m-%d", "%y.%m.%d", "%y/%m/%d",
]

# 이름 폴백 컬럼 (계약자명이 비어있을 때 대체로 사용)
NAME_FALLBACK_COLUMNS_TERMINATION = ["주민 번호", "상호"]
NAME_FALLBACK_COLUMNS_SETTLEMENT = []

# 이미 종료 처리된 건 키워드
ALREADY_TERMINATED_KEYWORDS = ["계약종료", "해지", "종료"]
TERMINATION_NOTE_TEMPLATE = "계약종료 (만기일: {expiry_date})"

# 컬럼명 매핑 (정산 시트 → 종료 시트)
# 정산 시트 컬럼명, 종료 시트 컬럼명
COL_SETTLEMENT_BRANCH = "지점명"
COL_SETTLEMENT_NAME = "계약자명"
COL_SETTLEMENT_ROOM = "호실"
COL_SETTLEMENT_START_DATE = "계약시작일자"
COL_SETTLEMENT_END_DATE = "계약종료일자"
COL_SETTLEMENT_BIZ_NUM = "사업자등록번호"
COL_SETTLEMENT_NOTE = "계약변경/해지/비고"

COL_TERMINATION_BRANCH = "지점명"
COL_TERMINATION_NAME = "계약자명"
COL_TERMINATION_ROOM = "호실"
COL_TERMINATION_START_DATE = "계약 시작 날짜"
COL_TERMINATION_END_DATE = "계약 만기 날짜"
COL_TERMINATION_BIZ_NUM = "사업자 등록번호"

# 내용 변경 감지용 필드 매핑 (정산컬럼, 종료컬럼, 표시이름, 비교타입)
FIELD_COMPARISONS = [
    ("결제금액",       "결제 금액",       "결제금액",     "number"),
    ("계약시작일자",   "계약 시작 날짜",  "계약시작일",   "date"),
    ("계약종료일자",   "계약 만기 날짜",  "계약종료일",   "date"),
    ("사업자등록번호", "사업자 등록번호", "사업자등록번호", "biz_number"),
    ("호실",          "호실",            "호실",         "text"),
]


class MatchStatus(Enum):
    EXACT = "정확매칭"
    FUZZY = "유사매칭(수동확인)"
    UNMATCHED = "매칭실패"


@dataclass
class MatchResult:
    termination_index: int
    settlement_index: Optional[int]
    status: MatchStatus
    confidence: float
    match_details: str
    termination_row: dict = field(default_factory=dict)
    settlement_row: dict = field(default_factory=dict)
    verification_passed: bool = False


# ============================================================
# 유틸리티 함수
# ============================================================

def normalize_text(text) -> str:
    if text is None:
        return ""
    text = str(text).strip()
    for char in NORMALIZE_REMOVE_CHARS:
        text = text.replace(char, "")
    return text.lower()


def normalize_name(name) -> str:
    if name is None:
        return ""
    name = str(name).strip()
    if not name:
        return ""
    name = re.sub(r"\s+", " ", name)
    if re.match(r"^[가-힣\s]+$", name):
        return name.replace(" ", "")
    if re.match(r"^[a-zA-Z\s.\-]+$", name):
        name = name.replace(".", " ").replace("-", " ")
        return re.sub(r"\s+", " ", name).strip().lower()
    return name.replace(" ", "").lower()


def is_likely_name(text) -> bool:
    if text is None:
        return False
    text = str(text).strip()
    if not text:
        return False
    if re.match(r"^[\d\-\s]+$", text):
        return False
    korean = re.sub(r"\s+", "", text)
    if re.match(r"^[가-힣]{2,5}$", korean):
        return True
    if re.match(r"^[a-zA-Z\s.\-]{2,}$", text) and any(c.isalpha() for c in text):
        return True
    return False


def normalize_business_number(biz_num) -> str:
    if biz_num is None:
        return ""
    return re.sub(r"[^0-9]", "", str(biz_num))


def parse_date(date_str) -> Optional[datetime]:
    if date_str is None or str(date_str).strip() == "":
        return None
    date_str = str(date_str).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def normalize_date_str(date_str) -> str:
    """날짜를 'YYYYMMDD' 형태의 정규화 문자열로 변환 (매칭 키 용도)"""
    dt = parse_date(date_str)
    if dt:
        return dt.strftime("%Y%m%d")
    return ""


def _get_next_column_value(row: dict, col_name: str) -> str:
    """주어진 컬럼명의 바로 다음 컬럼 값을 반환 (열 밀림 보정용)"""
    keys = list(row.keys())
    norm = col_name.replace(" ", "").replace("\n", "").strip()
    target_idx = -1
    for i, key in enumerate(keys):
        if key == col_name or str(key).replace(" ", "").replace("\n", "").strip() == norm:
            target_idx = i
            break
    if target_idx >= 0 and target_idx + 1 < len(keys):
        val = row[keys[target_idx + 1]]
        return str(val) if val is not None else ""
    return ""


def _get_termination_dates(row: dict) -> tuple[str, str]:
    """
    종료 시트에서 계약 시작/만기 날짜를 추출.
    열 밀림 보정 포함:
      정상: S열=시작날짜, T열=만기날짜
      밀림: S열=쓰레기, T열=시작날짜, U열=만기날짜
    """
    raw_start = get_col(row, COL_TERMINATION_START_DATE)  # S열
    raw_end = get_col(row, COL_TERMINATION_END_DATE)      # T열

    start_parsed = normalize_date_str(raw_start)
    end_parsed = normalize_date_str(raw_end)

    # 케이스 1: 둘 다 유효한 날짜 → 정상
    if start_parsed and end_parsed:
        return start_parsed, end_parsed

    # 케이스 2: S열 날짜X, T열 날짜O → 밀림 의심 → U열 확인
    if not start_parsed and end_parsed:
        next_col_val = _get_next_column_value(row, COL_TERMINATION_END_DATE)  # U열
        next_parsed = normalize_date_str(next_col_val)
        if next_parsed:
            # 밀림 확인됨: T=시작, U=만기
            # 추가 검증: 시작일 < 만기일
            if end_parsed <= next_parsed:
                return end_parsed, next_parsed
            # 시작 > 만기면 밀림이 아닐 수 있음, 원래대로
        # U열에 날짜가 없으면 T열을 만기일로 간주 (시작일 없음)
        return "", end_parsed

    # 케이스 3: S열 날짜O, T열 날짜X → S만 시작일 (이상한 경우)
    if start_parsed and not end_parsed:
        return start_parsed, ""

    # 케이스 4: 둘 다 없음 → 빈 값
    return "", ""


def get_col(row: dict, col_name: str) -> str:
    if col_name in row:
        return str(row[col_name]) if row[col_name] is not None else ""
    norm = col_name.replace(" ", "").replace("\n", "").strip()
    for key in row.keys():
        if str(key).replace(" ", "").replace("\n", "").strip() == norm:
            return str(row[key]) if row[key] is not None else ""
    return ""


def extract_name(row: dict, name_col: str, fallback_cols: list) -> str:
    name = get_col(row, name_col)
    if name.strip():
        return name.strip()
    for fb_col in fallback_cols:
        val = get_col(row, fb_col)
        if val.strip() and is_likely_name(val):
            return val.strip()
    return ""


# ============================================================
# 사전 정규화 인덱스 (PreparedRow)
# ============================================================

@dataclass
class PreparedRow:
    idx: int
    row: dict
    branch_norm: str
    name_raw: str
    name_norm: str
    room_norm: str
    start_date: str  # "YYYYMMDD" 또는 ""
    end_date: str    # "YYYYMMDD" 또는 ""


def _prepare_settlement(data: list[dict]) -> tuple[
    dict[tuple, list[PreparedRow]],    # exact_index: (branch, name) → rows
    dict[str, list[PreparedRow]],      # branch_index: branch → rows
    dict[tuple, list[PreparedRow]],    # date_index: (branch, start, end) → rows
    dict[tuple, list[PreparedRow]],    # end_date_index: (branch, end) → rows
]:
    exact_index = defaultdict(list)
    branch_index = defaultdict(list)
    date_index = defaultdict(list)
    end_date_index = defaultdict(list)

    for idx, row in enumerate(data):
        pr = PreparedRow(
            idx=idx, row=row,
            branch_norm=normalize_text(get_col(row, COL_SETTLEMENT_BRANCH)),
            name_raw=extract_name(row, COL_SETTLEMENT_NAME, NAME_FALLBACK_COLUMNS_SETTLEMENT),
            name_norm="",
            room_norm=normalize_text(get_col(row, COL_SETTLEMENT_ROOM)),
            start_date=normalize_date_str(get_col(row, COL_SETTLEMENT_START_DATE)),
            end_date=normalize_date_str(get_col(row, COL_SETTLEMENT_END_DATE)),
        )
        pr.name_norm = normalize_name(pr.name_raw)
        exact_index[(pr.branch_norm, pr.name_norm)].append(pr)
        branch_index[pr.branch_norm].append(pr)

        # 날짜 인덱스: 시작일+종료일 모두 있는 경우
        if pr.branch_norm and pr.start_date and pr.end_date:
            date_index[(pr.branch_norm, pr.start_date, pr.end_date)].append(pr)

        # 만기일 단독 인덱스: 만기일만 있어도 됨
        if pr.branch_norm and pr.end_date:
            end_date_index[(pr.branch_norm, pr.end_date)].append(pr)

    return exact_index, branch_index, date_index, end_date_index


def _prepare_termination(data: list[dict]) -> tuple[list[PreparedRow], dict]:
    """종료 데이터 전처리. 열 밀림 보정 포함.
    Returns: (prepared_rows, stats) — stats에 밀림 건수 등 포함
    """
    result = []
    shift_count = 0  # 밀림 보정된 행 수
    normal_date_count = 0  # 정상 날짜 행 수
    no_date_count = 0  # 날짜 없음

    for idx, row in enumerate(data):
        start_date, end_date = _get_termination_dates(row)

        # 밀림 감지 통계
        raw_start = normalize_date_str(get_col(row, COL_TERMINATION_START_DATE))
        raw_end = normalize_date_str(get_col(row, COL_TERMINATION_END_DATE))
        if not raw_start and raw_end and start_date == raw_end:
            # 밀림 보정됨: T→시작, U→만기
            shift_count += 1
        elif raw_start or raw_end:
            normal_date_count += 1
        else:
            no_date_count += 1

        pr = PreparedRow(
            idx=idx, row=row,
            branch_norm=normalize_text(get_col(row, COL_TERMINATION_BRANCH)),
            name_raw=extract_name(row, COL_TERMINATION_NAME, NAME_FALLBACK_COLUMNS_TERMINATION),
            name_norm="",
            room_norm=normalize_text(get_col(row, COL_TERMINATION_ROOM)),
            start_date=start_date,
            end_date=end_date,
        )
        pr.name_norm = normalize_name(pr.name_raw)
        result.append(pr)

    stats = {
        "total": len(data),
        "date_shift_corrected": shift_count,
        "normal_dates": normal_date_count,
        "no_dates": no_date_count,
    }
    return result, stats


# ============================================================
# rapidfuzz.process 기반 배치 인덱스
# ============================================================

def _build_fuzzy_index(branch_index: dict[str, list[PreparedRow]]) -> dict[str, tuple[list[str], list[PreparedRow]]]:
    fuzzy_idx = {}
    for branch_norm, rows in branch_index.items():
        if not branch_norm:
            continue
        names = [pr.name_norm for pr in rows]
        fuzzy_idx[branch_norm] = (names, rows)
    return fuzzy_idx


# ============================================================
# 매칭 엔진 v5 (날짜 기반 매칭 추가)
# ============================================================

def run_matching(
    settlement_data: list[dict],
    termination_data: list[dict],
    threshold: int = FUZZY_THRESHOLD,
) -> list[MatchResult]:
    t0 = time.time()

    exact_index, branch_index, date_index, end_date_index = _prepare_settlement(settlement_data)
    t_prepared, t_stats = _prepare_termination(termination_data)
    fuzzy_idx = _build_fuzzy_index(branch_index)

    print(f"[MATCHER v5.2] 종료시트 날짜 통계: "
          f"밀림보정={t_stats['date_shift_corrected']}, "
          f"정상날짜={t_stats['normal_dates']}, "
          f"날짜없음={t_stats['no_dates']}")

    results = []
    matched_indices: set[int] = set()

    rejected_count = 0  # 교차검증 거부 건수

    # === Phase 1: 정확 매칭 (지점 + 이름 + 호실) ===
    unmatched_after_exact = []
    for tp in t_prepared:
        result = _exact_match(tp, exact_index, matched_indices)
        if result is not None:
            verified = _cross_verify(result)
            if verified is not None:
                matched_indices.add(verified.settlement_index)
                results.append(verified)
            else:
                # 검증 거부됨 (만기일/호실 불일치) → 다음 Phase로
                rejected_count += 1
                unmatched_after_exact.append(tp)
        else:
            unmatched_after_exact.append(tp)

    # === Phase 2a: 날짜 기반 매칭 (지점 + 시작일 + 만기일 → 이름 유사도) ===
    unmatched_after_date = []
    for tp in unmatched_after_exact:
        result = _date_match(tp, date_index, end_date_index, matched_indices)
        if result is not None:
            verified = _cross_verify(result)
            if verified is not None:
                matched_indices.add(verified.settlement_index)
                results.append(verified)
            else:
                rejected_count += 1
                unmatched_after_date.append(tp)
        else:
            unmatched_after_date.append(tp)

    # === Phase 3: 퍼지 매칭 (rapidfuzz) ===
    fuzzy_start = time.time()
    for tp in unmatched_after_date:
        if time.time() - fuzzy_start > FUZZY_TIME_LIMIT:
            idx_pos = unmatched_after_date.index(tp)
            for remaining in unmatched_after_date[idx_pos:]:
                results.append(MatchResult(
                    termination_index=remaining.idx,
                    settlement_index=None,
                    status=MatchStatus.UNMATCHED,
                    confidence=0.0,
                    match_details="퍼지 매칭 시간 초과",
                    termination_row=remaining.row,
                ))
            break

        result = _fuzzy_match_fast(tp, fuzzy_idx, matched_indices, threshold)
        if result is not None:
            verified = _cross_verify(result)
            if verified is not None:
                matched_indices.add(verified.settlement_index)
                result = verified
            else:
                rejected_count += 1
                result = MatchResult(
                    termination_index=tp.idx,
                    settlement_index=None,
                    status=MatchStatus.UNMATCHED,
                    confidence=0.0,
                    match_details="검증 거부 (만기일/호실 불일치 → 다른 계약)",
                    termination_row=tp.row,
                )
        else:
            result = MatchResult(
                termination_index=tp.idx,
                settlement_index=None,
                status=MatchStatus.UNMATCHED,
                confidence=0.0,
                match_details="매칭되는 정산 건을 찾을 수 없음",
                termination_row=tp.row,
            )
        results.append(result)

    results.sort(key=lambda r: r.termination_index)

    # 매칭 통계 (디버그)
    matched_total = sum(1 for r in results if r.settlement_index is not None)
    print(f"[MATCHER v5.3] Phase1(이름): {len(t_prepared) - len(unmatched_after_exact)}, "
          f"Phase2(날짜): {len(unmatched_after_exact) - len(unmatched_after_date)}, "
          f"Phase3(퍼지): {matched_total - (len(t_prepared) - len(unmatched_after_date))}, "
          f"검증거부: {rejected_count}, "
          f"미매칭: {sum(1 for r in results if r.settlement_index is None)}/{len(t_prepared)}")

    return results


# ============================================================
# Phase 1: 정확 매칭 (지점 + 이름 [+ 호실])
# ============================================================

def _exact_match(tp: PreparedRow, exact_index, matched) -> Optional[MatchResult]:
    # 이름이 없으면 정확 매칭 불가 (빈 이름끼리 매칭 방지)
    if not tp.name_norm:
        return None
    if not tp.branch_norm:
        return None

    original = get_col(tp.row, COL_TERMINATION_NAME)
    used_fb = (not original.strip()) and tp.name_raw.strip()
    fb_note = " (폴백이름)" if used_fb else ""

    candidates = exact_index.get((tp.branch_norm, tp.name_norm), [])

    # 1차: 지점+이름+호실+날짜 모두 일치
    for sp in candidates:
        if sp.idx not in matched and tp.room_norm == sp.room_norm:
            if tp.start_date and sp.start_date and tp.start_date == sp.start_date:
                return MatchResult(
                    termination_index=tp.idx, settlement_index=sp.idx,
                    status=MatchStatus.EXACT, confidence=100.0,
                    match_details=f"정확매칭: {tp.branch_norm}/{tp.name_raw}{fb_note}/{tp.room_norm}/시작일일치",
                    termination_row=tp.row, settlement_row=sp.row,
                )

    # 2차: 지점+이름+호실 일치 (날짜 무관)
    for sp in candidates:
        if sp.idx not in matched and tp.room_norm == sp.room_norm:
            return MatchResult(
                termination_index=tp.idx, settlement_index=sp.idx,
                status=MatchStatus.EXACT, confidence=98.0,
                match_details=f"정확매칭: {tp.branch_norm}/{tp.name_raw}{fb_note}/{tp.room_norm}",
                termination_row=tp.row, settlement_row=sp.row,
            )

    # 3차: 지점+이름 일치, 호실 한쪽이 비어있음
    if not tp.room_norm:
        for sp in candidates:
            if sp.idx not in matched:
                return MatchResult(
                    termination_index=tp.idx, settlement_index=sp.idx,
                    status=MatchStatus.EXACT, confidence=90.0,
                    match_details=f"정확매칭(호실미확인): {tp.branch_norm}/{tp.name_raw}{fb_note}",
                    termination_row=tp.row, settlement_row=sp.row,
                )

    return None


# ============================================================
# Phase 2: 날짜 기반 매칭 (지점 + 시작일 + 만기일 → 이름 유사도 확인)
# ============================================================

def _date_match(tp: PreparedRow, date_index, end_date_index, matched) -> Optional[MatchResult]:
    """
    날짜 기반 매칭:
    1차: 지점 + 시작일 + 만기일 모두 일치 (가장 정확)
    2차: 지점 + 만기일만 일치 (종료 시트에 시작일이 3%만 있으므로)
    → 이름 유사도가 최소 기준 이상이면 매칭

    실제 데이터: 종료 시트 시작일 3.1%, 만기일 19.7%
    """
    if not tp.branch_norm:
        return None

    # 시작일+만기일 없으면 날짜 매칭 불가
    if not tp.start_date and not tp.end_date:
        return None

    candidates = []
    match_type = ""

    # 1차: 시작일+만기일 모두 일치 (가장 높은 신뢰도)
    if tp.start_date and tp.end_date:
        candidates = date_index.get((tp.branch_norm, tp.start_date, tp.end_date), [])
        match_type = "시작일+만기일"

    # 2차: 만기일만 일치 (종료 시트에 시작일이 거의 없으므로)
    if not candidates and tp.end_date:
        candidates = end_date_index.get((tp.branch_norm, tp.end_date), [])
        match_type = "만기일"

    if not candidates:
        return None

    # 후보 중 가장 유사한 이름 찾기 (+ 호실 보너스)
    best_sp = None
    best_score = 0

    for sp in candidates:
        if sp.idx in matched:
            continue

        score = 0

        # 이름 유사도
        if tp.name_norm and sp.name_norm:
            score = fuzz.ratio(tp.name_norm, sp.name_norm)
        elif not tp.name_norm and not sp.name_norm:
            score = 100
        # 한쪽만 이름이 있으면 score = 0

        # 호실 일치 보너스
        if tp.room_norm and sp.room_norm and tp.room_norm == sp.room_norm:
            score = min(100, score + 20)

        # 시작일도 일치하면 추가 보너스
        if tp.start_date and sp.start_date and tp.start_date == sp.start_date:
            score = min(100, score + 10)

        if score > best_score:
            best_score = score
            best_sp = sp

    if best_sp is None:
        return None

    # 이름 유사도가 너무 낮으면 거부 (날짜만 일치하고 이름이 완전 다르면 다른 계약)
    if best_score < DATE_MATCH_NAME_THRESHOLD:
        return None

    original = get_col(tp.row, COL_TERMINATION_NAME)
    fb = " [이름폴백]" if (not original.strip()) and tp.name_raw.strip() else ""

    # 날짜 포맷팅
    date_parts = []
    if tp.start_date:
        date_parts.append(f"시작={tp.start_date[:4]}-{tp.start_date[4:6]}-{tp.start_date[6:]}")
    if tp.end_date:
        date_parts.append(f"만기={tp.end_date[:4]}-{tp.end_date[4:6]}-{tp.end_date[6:]}")

    detail = f"날짜매칭({match_type}): {tp.branch_norm}/{'/'.join(date_parts)}/이름유사도={best_score:.0f}%{fb}"

    # 신뢰도 결정
    if match_type == "시작일+만기일":
        # 시작일+만기일 모두 일치 → 높은 신뢰도
        if best_score >= 70:
            confidence = 97.0
            status = MatchStatus.EXACT
        elif best_score >= 50:
            confidence = 90.0
            status = MatchStatus.EXACT
        else:
            confidence = 80.0
            status = MatchStatus.FUZZY
    else:
        # 만기일만 일치 → 중간 신뢰도
        if best_score >= 80:
            confidence = 92.0
            status = MatchStatus.EXACT
        elif best_score >= 60:
            confidence = 82.0
            status = MatchStatus.FUZZY
        else:
            confidence = 72.0
            status = MatchStatus.FUZZY

    return MatchResult(
        termination_index=tp.idx, settlement_index=best_sp.idx,
        status=status, confidence=confidence,
        match_details=detail,
        termination_row=tp.row, settlement_row=best_sp.row,
    )


# ============================================================
# Phase 3: 퍼지 매칭 (rapidfuzz + 날짜 보너스)
# ============================================================

def _fuzzy_match_fast(
    tp: PreparedRow, fuzzy_idx, matched, threshold,
) -> Optional[MatchResult]:
    if not tp.branch_norm or not tp.name_norm:
        return None

    entry = fuzzy_idx.get(tp.branch_norm)
    if not entry:
        return None

    names, prs = entry

    # 이름 최소 점수 (v4 호환: 지점30%+호실20% 제외 후 이름만 기준)
    name_cutoff = max(40, int((threshold - 30 - 20) / 0.5))

    best_match = rfprocess.extractOne(
        tp.name_norm, names,
        scorer=fuzz.ratio,
        score_cutoff=name_cutoff,
    )

    if not best_match:
        return None

    matched_name, name_score, match_idx = best_match
    sp = prs[match_idx]

    if sp.idx in matched:
        candidates = rfprocess.extract(
            tp.name_norm, names,
            scorer=fuzz.ratio,
            score_cutoff=name_cutoff,
            limit=10,
        )
        sp = None
        name_score = 0
        for cand_name, cand_score, cand_idx in candidates:
            if prs[cand_idx].idx not in matched:
                sp = prs[cand_idx]
                name_score = cand_score
                break
        if sp is None:
            return None

    # 호실 점수
    if tp.room_norm and sp.room_norm:
        rm = fuzz.ratio(tp.room_norm, sp.room_norm)
    else:
        rm = 100

    # 날짜 보너스 (v5: 있으면 보너스, 없으면 페널티 없음)
    date_bonus = 0
    date_detail = ""
    if tp.start_date and sp.start_date and tp.start_date == sp.start_date:
        date_bonus += 10
        date_detail += "시작일일치"
    if tp.end_date and sp.end_date and tp.end_date == sp.end_date:
        date_bonus += 10
        date_detail += "+만기일일치" if date_detail else "만기일일치"

    # 종합 점수 = v4 기본(지점30% + 이름50% + 호실20%) + 날짜보너스(최대 +20)
    # → 날짜 없이도 v4와 동일한 점수, 날짜 일치하면 보너스로 상승
    total = 100 * 0.3 + name_score * 0.5 + rm * 0.2 + date_bonus

    if total < threshold:
        return None

    original = get_col(tp.row, COL_TERMINATION_NAME)
    fb = " [이름폴백]" if (not original.strip()) and tp.name_raw.strip() else ""
    detail = f"유사매칭: 지점=100%, 이름={name_score:.0f}%{fb}, 호실={rm:.0f}%"
    if date_detail:
        detail += f", {date_detail}"
    detail += f" → {total:.1f}%"

    return MatchResult(
        termination_index=tp.idx, settlement_index=sp.idx,
        status=MatchStatus.FUZZY, confidence=total,
        match_details=detail,
        termination_row=tp.row, settlement_row=sp.row,
    )


# ============================================================
# 교차 검증
# ============================================================

def _cross_verify(result: MatchResult) -> Optional[MatchResult]:
    """
    교차 검증: 매칭된 쌍의 사업자번호, 시작일, 만기일을 비교.
    만기일 불일치 → 매칭 거부 (None 반환) — 같은 사람의 다른 계약 방지
    """
    verified = []
    hard_reject = False  # 매칭 자체를 거부할지 여부

    # 사업자 번호
    t_biz = normalize_business_number(get_col(result.termination_row, COL_TERMINATION_BIZ_NUM))
    s_biz = normalize_business_number(get_col(result.settlement_row, COL_SETTLEMENT_BIZ_NUM))
    if t_biz and s_biz:
        verified.append("사업자번호일치" if t_biz == s_biz else "사업자번호불일치")

    # 계약 시작일
    t_date = parse_date(get_col(result.termination_row, COL_TERMINATION_START_DATE))
    s_date = parse_date(get_col(result.settlement_row, COL_SETTLEMENT_START_DATE))
    if t_date and s_date:
        if t_date == s_date:
            verified.append("시작일일치")
        else:
            verified.append("시작일불일치")

    # 계약 만기일 — 핵심 검증 (다르면 다른 계약임)
    t_end = parse_date(get_col(result.termination_row, COL_TERMINATION_END_DATE))
    s_end = parse_date(get_col(result.settlement_row, COL_SETTLEMENT_END_DATE))
    if t_end and s_end:
        if t_end == s_end:
            verified.append("만기일일치")
        else:
            verified.append("만기일불일치")
            hard_reject = True  # ★ 만기일이 다르면 다른 계약 → 거부

    # 호실 검증 — 호실이 다르면 무조건 다른 계약 → 거부
    t_room = normalize_text(get_col(result.termination_row, COL_TERMINATION_ROOM))
    s_room = normalize_text(get_col(result.settlement_row, COL_SETTLEMENT_ROOM))
    if t_room and s_room:
        if t_room == s_room:
            verified.append("호실일치")
        else:
            verified.append("호실불일치")
            hard_reject = True  # ★ 호실 다르면 무조건 거부 (같은 사람의 다른 호실 계약)

    # 만기일이 다르면 매칭 자체를 거부 (같은 사람의 다른 계약)
    if hard_reject:
        result.verification_passed = False
        result.match_details += f" [검증거부: {','.join(verified)}]"
        return None  # ★ 매칭 거부

    has_ok = any("일치" in v and "불일치" not in v for v in verified)
    has_ng = any("불일치" in v for v in verified)

    if has_ok and not has_ng:
        result.verification_passed = True
        result.match_details += f" [검증통과: {','.join(verified)}]"
    elif has_ng:
        result.verification_passed = False
        result.match_details += f" [검증실패: {','.join(verified)}]"
        if result.status == MatchStatus.EXACT:
            result.status = MatchStatus.FUZZY
    else:
        result.verification_passed = True
        result.match_details += " [검증데이터없음]"

    return result


# ============================================================
# 내용 변경 감지
# ============================================================

def _normalize_number(val: str) -> str:
    if not val or not val.strip():
        return ""
    cleaned = re.sub(r"[^\d.\-]", "", val.strip())
    if not cleaned:
        return ""
    try:
        num = float(cleaned)
        return str(int(num)) if num == int(num) else str(num)
    except ValueError:
        return cleaned


def _normalize_date_str(val: str) -> str:
    dt = parse_date(val)
    return dt.strftime("%Y-%m-%d") if dt else (val.strip() if val else "")


def _normalize_for_compare(val: str, compare_type: str) -> str:
    if compare_type == "number":
        return _normalize_number(val)
    elif compare_type == "date":
        return _normalize_date_str(val)
    elif compare_type == "biz_number":
        return normalize_business_number(val)
    return normalize_text(val)


@dataclass
class FieldDiff:
    field_name: str
    settlement_value: str
    termination_value: str


@dataclass
class ChangeResult:
    settlement_index: int
    termination_index: int
    branch: str
    contractor: str
    room: str
    diffs: list[FieldDiff]
    settlement_row: dict = field(default_factory=dict)
    termination_row: dict = field(default_factory=dict)


def detect_changes(match_results: list[MatchResult]) -> list[ChangeResult]:
    changes = []
    for r in match_results:
        if r.settlement_index is None or not r.settlement_row or not r.termination_row:
            continue
        diffs = []
        for s_col, t_col, display_name, cmp_type in FIELD_COMPARISONS:
            s_raw = get_col(r.settlement_row, s_col)
            t_raw = get_col(r.termination_row, t_col)
            s_norm = _normalize_for_compare(s_raw, cmp_type)
            t_norm = _normalize_for_compare(t_raw, cmp_type)
            if not s_norm and not t_norm:
                continue
            if s_norm != t_norm:
                diffs.append(FieldDiff(
                    field_name=display_name,
                    settlement_value=s_raw.strip() if s_raw else "(없음)",
                    termination_value=t_raw.strip() if t_raw else "(없음)",
                ))
        if diffs:
            changes.append(ChangeResult(
                settlement_index=r.settlement_index,
                termination_index=r.termination_index,
                branch=get_col(r.termination_row, COL_TERMINATION_BRANCH),
                contractor=extract_name(r.termination_row, COL_TERMINATION_NAME, NAME_FALLBACK_COLUMNS_TERMINATION),
                room=get_col(r.termination_row, COL_TERMINATION_ROOM),
                diffs=diffs,
                settlement_row=r.settlement_row,
                termination_row=r.termination_row,
            ))
    return changes


def changes_to_json(changes: list[ChangeResult]) -> list[dict]:
    return [{
        "settlement_index": c.settlement_index,
        "termination_index": c.termination_index,
        "branch": c.branch,
        "contractor": c.contractor,
        "room": c.room,
        "diffs": [{"field": d.field_name, "settlement": d.settlement_value, "termination": d.termination_value} for d in c.diffs],
    } for c in changes]


def changes_summary(changes: list[ChangeResult]) -> dict:
    field_counts = defaultdict(int)
    for c in changes:
        for d in c.diffs:
            field_counts[d.field_name] += 1
    return {"total_changed": len(changes), "by_field": dict(field_counts)}


# ============================================================
# 결과 → JSON 직렬화
# ============================================================

def results_to_json(results: list[MatchResult]) -> list[dict]:
    out = []
    for r in results:
        # 종료시트 날짜는 밀림 보정된 값 사용
        t_start_corr, t_end_corr = _get_termination_dates(r.termination_row)
        out.append({
            "termination_index": r.termination_index,
            "settlement_index": r.settlement_index,
            "status": r.status.value,
            "confidence": round(r.confidence, 1),
            "match_details": r.match_details,
            "verification_passed": r.verification_passed,
            "t_branch": get_col(r.termination_row, COL_TERMINATION_BRANCH),
            "t_contractor": extract_name(r.termination_row, COL_TERMINATION_NAME, NAME_FALLBACK_COLUMNS_TERMINATION),
            "t_room": get_col(r.termination_row, COL_TERMINATION_ROOM),
            "t_expiry": t_end_corr or get_col(r.termination_row, COL_TERMINATION_END_DATE),
            "t_start": t_start_corr or get_col(r.termination_row, COL_TERMINATION_START_DATE),
            "t_biz_num": get_col(r.termination_row, COL_TERMINATION_BIZ_NUM),
            "s_branch": get_col(r.settlement_row, COL_SETTLEMENT_BRANCH) if r.settlement_row else "",
            "s_contractor": get_col(r.settlement_row, COL_SETTLEMENT_NAME) if r.settlement_row else "",
            "s_room": get_col(r.settlement_row, COL_SETTLEMENT_ROOM) if r.settlement_row else "",
            "s_biz_num": get_col(r.settlement_row, COL_SETTLEMENT_BIZ_NUM) if r.settlement_row else "",
            "s_note": get_col(r.settlement_row, COL_SETTLEMENT_NOTE) if r.settlement_row else "",
            "s_end_date": get_col(r.settlement_row, COL_SETTLEMENT_END_DATE) if r.settlement_row else "",
        })
    return out


def summary(results: list[MatchResult]) -> dict:
    total = len(results)
    exact = sum(1 for r in results if r.status == MatchStatus.EXACT)
    fuzzy_ = sum(1 for r in results if r.status == MatchStatus.FUZZY)
    unmatched = sum(1 for r in results if r.status == MatchStatus.UNMATCHED)
    return {"total": total, "exact": exact, "fuzzy": fuzzy_, "unmatched": unmatched}
