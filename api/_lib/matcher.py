"""
매칭 엔진 v6 — 호실 + 이름 + 날짜 기반 매칭

매칭 전략 (호실이 가장 강한 키):
  Phase 1: 지점 + 호실 + 이름 + 만기일 → 신뢰도 100% (완벽매칭)
  Phase 2: 지점 + 호실 + 이름 (날짜무관) → 신뢰도 95% (정확매칭)
  Phase 3: 지점 + 호실 + 만기일 → 이름 유사도 확인 (이름 오타/변형)
  Phase 4: 지점 + 이름 + 만기일 → 호실 다를 수 있음 (호실 변경)
  Phase 5: 지점 + 이름만 → 호실/날짜 부재 시 최종 매칭

교차검증: 완화 (거부 없이 신뢰도 조정)
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
FUZZY_THRESHOLD = 75          # 최종 매칭 임계값 (%)
FUZZY_TIME_LIMIT = 15         # 퍼지 매칭 최대 시간 (초)
NAME_FUZZY_CUTOFF = 60        # 이름 유사도 최소값
ROOM_FUZZY_CUTOFF = 80        # 호실 유사도 최소값

NORMALIZE_REMOVE_CHARS = [" ", "\t", "\n", "-", ".", "(", ")", "\u3000"]
DATE_FORMATS = [
    "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d",
    "%y-%m-%d", "%y.%m.%d", "%y/%m/%d",
]

# 이름 폴백 컬럼
NAME_FALLBACK_COLUMNS_TERMINATION = ["주민 번호", "상호"]
NAME_FALLBACK_COLUMNS_SETTLEMENT = []

# 이미 종료 처리된 건 키워드
ALREADY_TERMINATED_KEYWORDS = ["계약종료", "해지", "종료"]
TERMINATION_NOTE_TEMPLATE = "계약종료 (만기일: {expiry_date})"

# ============================================================
# 컬럼명 매핑
# ============================================================
# 정산 시트 (A~P)
COL_SETTLEMENT_BRANCH = "지점명"          # C
COL_SETTLEMENT_NAME = "계약자명"          # D
COL_SETTLEMENT_START_DATE = "계약시작일자"  # E
COL_SETTLEMENT_END_DATE = "계약종료일자"    # F
COL_SETTLEMENT_BIZ_NUM = "사업자등록번호"   # N
COL_SETTLEMENT_ROOM = "호실"              # O
COL_SETTLEMENT_NOTE = "계약변경/해지/비고"  # J

# 종료 시트 (A~AD)
COL_TERMINATION_BRANCH = "지점명"          # A
COL_TERMINATION_ROOM = "호실"              # H
COL_TERMINATION_NAME = "계약자명"          # I
COL_TERMINATION_BIZ_NUM = "사업자 등록번호"  # P
COL_TERMINATION_START_DATE = "계약 시작 날짜"  # S
COL_TERMINATION_END_DATE = "계약 만기 날짜"    # T

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


def normalize_room(room) -> str:
    """호실 정규화: 공백/특문 제거, 소문자화"""
    if room is None:
        return ""
    room = str(room).strip()
    if not room:
        return ""
    # 공백, 하이픈 제거 후 소문자
    room = re.sub(r"[\s\-\.]", "", room).lower()
    return room


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
    """날짜를 'YYYYMMDD' 정규화 문자열로 변환"""
    dt = parse_date(date_str)
    if dt:
        return dt.strftime("%Y%m%d")
    return ""


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
# 날짜 밀림 보정
# ============================================================

def _get_next_column_value(row: dict, col_name: str) -> str:
    """주어진 컬럼명의 바로 다음 컬럼 값 반환 (열 밀림 보정용)"""
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
    종료 시트에서 계약 시작/만기 날짜를 추출 (열 밀림 보정 포함).
    정상: S열=시작날짜, T열=만기날짜
    밀림: S열=쓰레기, T열=시작날짜, U열=만기날짜
    """
    raw_start = get_col(row, COL_TERMINATION_START_DATE)  # S열
    raw_end = get_col(row, COL_TERMINATION_END_DATE)      # T열

    start_parsed = normalize_date_str(raw_start)
    end_parsed = normalize_date_str(raw_end)

    # 케이스 1: 둘 다 유효 → 정상
    if start_parsed and end_parsed:
        return start_parsed, end_parsed

    # 케이스 2: S열 날짜X, T열 날짜O → 밀림 의심 → U열 확인
    if not start_parsed and end_parsed:
        next_col_val = _get_next_column_value(row, COL_TERMINATION_END_DATE)
        next_parsed = normalize_date_str(next_col_val)
        if next_parsed and end_parsed <= next_parsed:
            return end_parsed, next_parsed
        return "", end_parsed

    # 케이스 3: S열만 유효
    if start_parsed and not end_parsed:
        return start_parsed, ""

    # 케이스 4: 둘 다 없음
    return "", ""


# ============================================================
# 데이터 준비 (PreparedRow)
# ============================================================

@dataclass
class PreparedRow:
    idx: int
    row: dict
    branch_norm: str
    name_raw: str
    name_norm: str
    room_norm: str
    start_date: str   # "YYYYMMDD" 또는 ""
    end_date: str     # "YYYYMMDD" 또는 ""


def _prepare_settlement(data: list[dict]) -> dict:
    """정산 데이터를 여러 인덱스로 준비"""
    all_rows = []
    # 인덱스들
    branch_room_name = defaultdict(list)  # (branch, room, name) → rows
    branch_room = defaultdict(list)       # (branch, room) → rows
    branch_name = defaultdict(list)       # (branch, name) → rows
    branch_only = defaultdict(list)       # branch → rows

    for idx, row in enumerate(data):
        pr = PreparedRow(
            idx=idx, row=row,
            branch_norm=normalize_text(get_col(row, COL_SETTLEMENT_BRANCH)),
            name_raw=extract_name(row, COL_SETTLEMENT_NAME, NAME_FALLBACK_COLUMNS_SETTLEMENT),
            name_norm="",
            room_norm=normalize_room(get_col(row, COL_SETTLEMENT_ROOM)),
            start_date=normalize_date_str(get_col(row, COL_SETTLEMENT_START_DATE)),
            end_date=normalize_date_str(get_col(row, COL_SETTLEMENT_END_DATE)),
        )
        pr.name_norm = normalize_name(pr.name_raw)
        all_rows.append(pr)

        if pr.branch_norm:
            branch_only[pr.branch_norm].append(pr)
            if pr.name_norm:
                branch_name[(pr.branch_norm, pr.name_norm)].append(pr)
            if pr.room_norm:
                branch_room[(pr.branch_norm, pr.room_norm)].append(pr)
                if pr.name_norm:
                    branch_room_name[(pr.branch_norm, pr.room_norm, pr.name_norm)].append(pr)

    return {
        "all": all_rows,
        "branch_room_name": dict(branch_room_name),
        "branch_room": dict(branch_room),
        "branch_name": dict(branch_name),
        "branch_only": dict(branch_only),
    }


def _prepare_termination(data: list[dict]) -> tuple[list[PreparedRow], dict]:
    """종료 데이터 전처리 (열 밀림 보정 포함)"""
    result = []
    shift_count = 0
    normal_date_count = 0
    no_date_count = 0

    for idx, row in enumerate(data):
        start_date, end_date = _get_termination_dates(row)

        # 밀림 감지 통계
        raw_start = normalize_date_str(get_col(row, COL_TERMINATION_START_DATE))
        raw_end = normalize_date_str(get_col(row, COL_TERMINATION_END_DATE))
        if not raw_start and raw_end and start_date == raw_end:
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
            room_norm=normalize_room(get_col(row, COL_TERMINATION_ROOM)),
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
# 매칭 점수 계산 헬퍼
# ============================================================

def _date_match_score(tp: PreparedRow, sp: PreparedRow) -> tuple[int, str]:
    """날짜 일치 점수와 설명 반환"""
    score = 0
    detail_parts = []

    if tp.end_date and sp.end_date:
        if tp.end_date == sp.end_date:
            score += 15
            detail_parts.append("만기일일치")
        else:
            detail_parts.append("만기일불일치")

    if tp.start_date and sp.start_date:
        if tp.start_date == sp.start_date:
            score += 10
            detail_parts.append("시작일일치")

    return score, "+".join(detail_parts) if detail_parts else "날짜정보없음"


def _pick_best_by_date(tp: PreparedRow, candidates: list[PreparedRow], matched: set) -> Optional[PreparedRow]:
    """후보 중 날짜가 가장 잘 맞는 행 선택"""
    best = None
    best_score = -1

    for sp in candidates:
        if sp.idx in matched:
            continue
        score, _ = _date_match_score(tp, sp)
        if score > best_score:
            best_score = score
            best = sp

    # 날짜 점수가 0이면 첫 번째 미매칭 행 반환
    if best is None:
        for sp in candidates:
            if sp.idx not in matched:
                return sp
    return best


# ============================================================
# 매칭 엔진 v6
# ============================================================

def run_matching(
    settlement_data: list[dict],
    termination_data: list[dict],
    threshold: int = FUZZY_THRESHOLD,
) -> list[MatchResult]:
    t0 = time.time()

    s_idx = _prepare_settlement(settlement_data)
    t_prepared, t_stats = _prepare_termination(termination_data)

    print(f"[MATCHER v6] 종료시트: {len(t_prepared)}행, "
          f"밀림보정={t_stats['date_shift_corrected']}, "
          f"정상날짜={t_stats['normal_dates']}, "
          f"날짜없음={t_stats['no_dates']}")
    print(f"[MATCHER v6] 정산 인덱스: "
          f"branch_room_name={len(s_idx['branch_room_name'])}, "
          f"branch_room={len(s_idx['branch_room'])}, "
          f"branch_name={len(s_idx['branch_name'])}, "
          f"branches={len(s_idx['branch_only'])}")

    results = []
    matched_s: set[int] = set()  # 이미 매칭된 정산 인덱스

    phase_counts = {"p1_room_name_date": 0, "p2_room_name": 0,
                    "p3_room_date": 0, "p4_name_date": 0,
                    "p5_name_only": 0, "unmatched": 0}

    # === Phase 1: 지점 + 호실 + 이름 + 만기일 (완벽매칭) ===
    unmatched = []
    for tp in t_prepared:
        if not tp.branch_norm or not tp.room_norm or not tp.name_norm:
            unmatched.append(tp)
            continue

        key = (tp.branch_norm, tp.room_norm, tp.name_norm)
        candidates = s_idx["branch_room_name"].get(key, [])

        best = _pick_best_by_date(tp, candidates, matched_s)
        if best is not None:
            date_score, date_detail = _date_match_score(tp, best)
            if date_score > 0:
                matched_s.add(best.idx)
                results.append(MatchResult(
                    termination_index=tp.idx, settlement_index=best.idx,
                    status=MatchStatus.EXACT, confidence=100.0,
                    match_details=f"P1: 지점+호실+이름+{date_detail}",
                    termination_row=tp.row, settlement_row=best.row,
                    verification_passed=True,
                ))
                phase_counts["p1_room_name_date"] += 1
                continue

        unmatched.append(tp)

    # === Phase 2: 지점 + 호실 + 이름 (날짜 무관) ===
    unmatched2 = []
    for tp in unmatched:
        if not tp.branch_norm or not tp.room_norm or not tp.name_norm:
            unmatched2.append(tp)
            continue

        key = (tp.branch_norm, tp.room_norm, tp.name_norm)
        candidates = s_idx["branch_room_name"].get(key, [])

        best = _pick_best_by_date(tp, candidates, matched_s)
        if best is not None:
            date_score, date_detail = _date_match_score(tp, best)
            matched_s.add(best.idx)
            conf = 95.0 + (date_score * 0.2)  # 날짜 보너스
            results.append(MatchResult(
                termination_index=tp.idx, settlement_index=best.idx,
                status=MatchStatus.EXACT, confidence=min(99.0, conf),
                match_details=f"P2: 지점+호실+이름 ({date_detail})",
                termination_row=tp.row, settlement_row=best.row,
                verification_passed=True,
            ))
            phase_counts["p2_room_name"] += 1
            continue

        unmatched2.append(tp)

    # === Phase 3: 지점 + 호실 + 만기일 → 이름 유사도 확인 ===
    unmatched3 = []
    for tp in unmatched2:
        if not tp.branch_norm or not tp.room_norm:
            unmatched3.append(tp)
            continue

        room_candidates = s_idx["branch_room"].get((tp.branch_norm, tp.room_norm), [])
        if not room_candidates:
            unmatched3.append(tp)
            continue

        best_sp = None
        best_score = 0
        best_name_sim = 0

        for sp in room_candidates:
            if sp.idx in matched_s:
                continue

            # 만기일 일치 확인
            date_score, _ = _date_match_score(tp, sp)

            # 이름 유사도
            name_sim = 0
            if tp.name_norm and sp.name_norm:
                name_sim = fuzz.ratio(tp.name_norm, sp.name_norm)
            elif not tp.name_norm and not sp.name_norm:
                name_sim = 100

            # 종합 점수: 만기일 일치가 중요, 이름 유사도는 보조
            total = date_score + name_sim * 0.5
            if total > best_score and name_sim >= 30:
                best_score = total
                best_sp = sp
                best_name_sim = name_sim

        if best_sp is not None and (best_name_sim >= NAME_FUZZY_CUTOFF or best_score >= 40):
            matched_s.add(best_sp.idx)
            date_score, date_detail = _date_match_score(tp, best_sp)
            conf = 80.0 + min(15.0, date_score) + min(5.0, best_name_sim * 0.05)
            status = MatchStatus.EXACT if best_name_sim >= 80 else MatchStatus.FUZZY
            results.append(MatchResult(
                termination_index=tp.idx, settlement_index=best_sp.idx,
                status=status, confidence=min(98.0, conf),
                match_details=f"P3: 지점+호실+{date_detail}/이름유사도={best_name_sim:.0f}%",
                termination_row=tp.row, settlement_row=best_sp.row,
                verification_passed=best_name_sim >= 70,
            ))
            phase_counts["p3_room_date"] += 1
            continue

        unmatched3.append(tp)

    # === Phase 4: 지점 + 이름 + 만기일 (호실이 다를 수 있음) ===
    unmatched4 = []
    for tp in unmatched3:
        if not tp.branch_norm or not tp.name_norm:
            unmatched4.append(tp)
            continue

        name_candidates = s_idx["branch_name"].get((tp.branch_norm, tp.name_norm), [])
        if not name_candidates:
            unmatched4.append(tp)
            continue

        best_sp = None
        best_total = 0
        best_room_sim = 0

        for sp in name_candidates:
            if sp.idx in matched_s:
                continue

            date_score, _ = _date_match_score(tp, sp)

            # 호실 유사도 (있으면 보너스)
            room_sim = 0
            if tp.room_norm and sp.room_norm:
                room_sim = fuzz.ratio(tp.room_norm, sp.room_norm)
            elif not tp.room_norm or not sp.room_norm:
                room_sim = 50  # 한쪽 없으면 중립

            total = date_score + room_sim * 0.3
            if total > best_total:
                best_total = total
                best_sp = sp
                best_room_sim = room_sim

        if best_sp is not None:
            date_score, date_detail = _date_match_score(tp, best_sp)
            # 만기일 일치하면 호실이 달라도 매칭 (같은 사람이 호실 변경)
            if date_score >= 10 or best_room_sim >= 80:
                matched_s.add(best_sp.idx)
                conf = 75.0 + min(15.0, date_score) + min(10.0, best_room_sim * 0.1)
                status = MatchStatus.EXACT if (date_score >= 15 and best_room_sim >= 80) else MatchStatus.FUZZY
                room_info = f"호실유사도={best_room_sim:.0f}%" if tp.room_norm and best_sp.room_norm else "호실미확인"
                results.append(MatchResult(
                    termination_index=tp.idx, settlement_index=best_sp.idx,
                    status=status, confidence=min(95.0, conf),
                    match_details=f"P4: 지점+이름+{date_detail}/{room_info}",
                    termination_row=tp.row, settlement_row=best_sp.row,
                    verification_passed=date_score >= 15,
                ))
                phase_counts["p4_name_date"] += 1
                continue

        unmatched4.append(tp)

    # === Phase 5: 지점 + 이름만 (최종 퍼지) ===
    fuzzy_start = time.time()
    for tp in unmatched4:
        if time.time() - fuzzy_start > FUZZY_TIME_LIMIT:
            idx_pos = unmatched4.index(tp)
            for remaining in unmatched4[idx_pos:]:
                results.append(MatchResult(
                    termination_index=remaining.idx, settlement_index=None,
                    status=MatchStatus.UNMATCHED, confidence=0.0,
                    match_details="시간초과", termination_row=remaining.row,
                ))
                phase_counts["unmatched"] += 1
            break

        if not tp.branch_norm or not tp.name_norm:
            results.append(MatchResult(
                termination_index=tp.idx, settlement_index=None,
                status=MatchStatus.UNMATCHED, confidence=0.0,
                match_details="지점명 또는 이름 없음",
                termination_row=tp.row,
            ))
            phase_counts["unmatched"] += 1
            continue

        name_candidates = s_idx["branch_name"].get((tp.branch_norm, tp.name_norm), [])

        if name_candidates:
            best = _pick_best_by_date(tp, name_candidates, matched_s)
            if best is not None:
                date_score, date_detail = _date_match_score(tp, best)
                room_sim = 0
                if tp.room_norm and best.room_norm:
                    room_sim = fuzz.ratio(tp.room_norm, best.room_norm)

                conf = 70.0 + date_score * 0.5 + room_sim * 0.1
                if conf >= threshold:
                    matched_s.add(best.idx)
                    room_info = f"호실유사도={room_sim:.0f}%" if tp.room_norm and best.room_norm else "호실미확인"
                    results.append(MatchResult(
                        termination_index=tp.idx, settlement_index=best.idx,
                        status=MatchStatus.FUZZY, confidence=min(90.0, conf),
                        match_details=f"P5: 지점+이름/{date_detail}/{room_info}",
                        termination_row=tp.row, settlement_row=best.row,
                        verification_passed=False,
                    ))
                    phase_counts["p5_name_only"] += 1
                    continue

        # 퍼지 이름 검색 (이름이 정확히 안 맞을 때)
        branch_rows = s_idx["branch_only"].get(tp.branch_norm, [])
        if branch_rows:
            names = [pr.name_norm for pr in branch_rows]
            best_match = rfprocess.extractOne(
                tp.name_norm, names,
                scorer=fuzz.ratio,
                score_cutoff=NAME_FUZZY_CUTOFF,
            )
            if best_match:
                _, name_score, m_idx = best_match
                sp = branch_rows[m_idx]

                if sp.idx not in matched_s:
                    date_score, date_detail = _date_match_score(tp, sp)
                    room_sim = 0
                    if tp.room_norm and sp.room_norm:
                        room_sim = fuzz.ratio(tp.room_norm, sp.room_norm)

                    # 호실+이름+날짜 종합 점수
                    total = (name_score * 0.4) + (room_sim * 0.3 if tp.room_norm and sp.room_norm else 30) + date_score + 30
                    if total >= threshold:
                        matched_s.add(sp.idx)
                        room_info = f"호실={room_sim:.0f}%" if tp.room_norm and sp.room_norm else ""
                        results.append(MatchResult(
                            termination_index=tp.idx, settlement_index=sp.idx,
                            status=MatchStatus.FUZZY,
                            confidence=min(85.0, total),
                            match_details=f"P5-fuzzy: 이름={name_score:.0f}%/{room_info}/{date_detail} →{total:.0f}%",
                            termination_row=tp.row, settlement_row=sp.row,
                            verification_passed=False,
                        ))
                        phase_counts["p5_name_only"] += 1
                        continue

        results.append(MatchResult(
            termination_index=tp.idx, settlement_index=None,
            status=MatchStatus.UNMATCHED, confidence=0.0,
            match_details="매칭 실패 (정산시트에 해당 계약 없음)",
            termination_row=tp.row,
        ))
        phase_counts["unmatched"] += 1

    results.sort(key=lambda r: r.termination_index)

    # 매칭 통계
    matched_total = sum(1 for r in results if r.settlement_index is not None)
    elapsed = round(time.time() - t0, 2)
    print(f"[MATCHER v6] 결과: 전체={len(results)}, 매칭={matched_total}, "
          f"P1={phase_counts['p1_room_name_date']}, "
          f"P2={phase_counts['p2_room_name']}, "
          f"P3={phase_counts['p3_room_date']}, "
          f"P4={phase_counts['p4_name_date']}, "
          f"P5={phase_counts['p5_name_only']}, "
          f"미매칭={phase_counts['unmatched']}, "
          f"소요={elapsed}s")

    return results


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
