"""
매칭 엔진 모듈 (Vercel 서버리스용) — 초경량 버전

pandas 제거, 순수 dict 리스트 기반으로 동작합니다.
1차 정확 매칭 (인덱스 기반 O(1)) → 퍼지 매칭 → 교차 검증
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from rapidfuzz import fuzz

# ============================================================
# 설정 상수
# ============================================================
FUZZY_THRESHOLD = 80
NORMALIZE_REMOVE_CHARS = [" ", "\t", "\n", "-", ".", "(", ")", "\u3000"]
DATE_FORMATS = [
    "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d",
    "%y-%m-%d", "%y.%m.%d", "%y/%m/%d",
]
NAME_FALLBACK_COLUMNS_TERMINATION = ["주민 번호", "상호"]
NAME_FALLBACK_COLUMNS_SETTLEMENT = []
ALREADY_TERMINATED_KEYWORDS = ["계약종료", "해지", "종료"]
TERMINATION_NOTE_TEMPLATE = "계약종료 (만기일: {expiry_date})"


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


def get_col(row: dict, col_name: str) -> str:
    """행에서 컬럼값을 유연하게 가져옴 (공백/줄바꿈 차이 무시)"""
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
# 매칭 엔진 (dict 리스트 기반, pandas 불필요)
# ============================================================

def run_matching(
    settlement_data: list[dict],
    termination_data: list[dict],
    threshold: int = FUZZY_THRESHOLD,
) -> list[MatchResult]:
    """전체 매칭 실행"""
    results = []
    matched_indices: set[int] = set()

    # 정산 시트 사전 인덱싱
    s_index = defaultdict(list)
    s_prepared = []
    for s_idx, s_dict in enumerate(settlement_data):
        s_branch = normalize_text(get_col(s_dict, "지점명"))
        s_name_raw = extract_name(s_dict, "계약자명", NAME_FALLBACK_COLUMNS_SETTLEMENT)
        s_name = normalize_name(s_name_raw)
        s_room = normalize_text(get_col(s_dict, "호실"))
        s_index[(s_branch, s_name)].append((s_idx, s_dict, s_room, s_name_raw))
        s_prepared.append((s_idx, s_dict, get_col(s_dict, "지점명"), s_name_raw, get_col(s_dict, "호실")))

    for t_idx, t_dict in enumerate(termination_data):
        result = _exact_match(t_idx, t_dict, s_index, matched_indices)
        if result is None:
            result = _fuzzy_match(t_idx, t_dict, s_prepared, matched_indices, threshold)
        if result is None:
            result = MatchResult(
                termination_index=t_idx,
                settlement_index=None,
                status=MatchStatus.UNMATCHED,
                confidence=0.0,
                match_details="매칭되는 정산 건을 찾을 수 없음",
                termination_row=t_dict,
            )

        if result.settlement_index is not None:
            result = _cross_verify(result)
            matched_indices.add(result.settlement_index)

        results.append(result)

    return results


def _exact_match(t_idx, t_row, s_index, matched) -> Optional[MatchResult]:
    t_branch = normalize_text(get_col(t_row, "지점명"))
    t_room = normalize_text(get_col(t_row, "호실"))
    t_name_raw = extract_name(t_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION)
    t_name = normalize_name(t_name_raw)

    if not t_branch and not t_name:
        return None

    original = get_col(t_row, "계약자명")
    used_fb = (not original.strip()) and t_name_raw.strip()
    fb_note = " (주민번호칸 이름)" if used_fb else ""

    candidates = s_index.get((t_branch, t_name), [])

    # 호실까지 일치하는 것 먼저
    for s_idx, s_dict, s_room, _ in candidates:
        if s_idx in matched:
            continue
        if t_room == s_room:
            return MatchResult(
                termination_index=t_idx, settlement_index=s_idx,
                status=MatchStatus.EXACT, confidence=100.0,
                match_details=f"정확매칭: {t_branch}/{t_name_raw}{fb_note}/{t_room}",
                termination_row=t_row, settlement_row=s_dict,
            )

    # 호실 비어있는 경우
    for s_idx, s_dict, s_room, _ in candidates:
        if s_idx in matched:
            continue
        if not t_room or not s_room:
            return MatchResult(
                termination_index=t_idx, settlement_index=s_idx,
                status=MatchStatus.EXACT, confidence=90.0,
                match_details=f"정확매칭(호실미확인): {t_branch}/{t_name_raw}{fb_note}",
                termination_row=t_row, settlement_row=s_dict,
            )

    return None


def _fuzzy_match(t_idx, t_row, s_prepared, matched, threshold) -> Optional[MatchResult]:
    t_branch = get_col(t_row, "지점명")
    t_name_raw = extract_name(t_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION)
    t_room = get_col(t_row, "호실")

    if not t_branch.strip() and not t_name_raw.strip():
        return None

    original = get_col(t_row, "계약자명")
    used_fb = (not original.strip()) and t_name_raw.strip()

    t_branch_norm = normalize_text(t_branch)
    t_name_norm = normalize_name(t_name_raw)
    t_room_norm = normalize_text(t_room)

    best_score, best_idx, best_row, best_detail = 0.0, None, None, ""

    for s_idx, s_dict, s_branch, s_name_raw, s_room in s_prepared:
        if s_idx in matched:
            continue

        s_branch_norm = normalize_text(s_branch)

        # 지점명이 완전히 다르면 스킵 (조기 종료로 속도 향상)
        br = fuzz.ratio(t_branch_norm, s_branch_norm) if t_branch_norm and s_branch_norm else 0
        if br < 50:
            continue

        s_name_norm = normalize_name(s_name_raw)
        s_room_norm = normalize_text(s_room)

        cn = max(fuzz.ratio(t_name_norm, s_name_norm), fuzz.token_sort_ratio(t_name_norm, s_name_norm)) if t_name_norm and s_name_norm else 0
        rm = fuzz.ratio(t_room_norm, s_room_norm) if t_room_norm and s_room_norm else 100

        total = br * 0.3 + cn * 0.5 + rm * 0.2
        if total > best_score:
            best_score, best_idx, best_row = total, s_idx, s_dict
            fb = " [이름폴백]" if used_fb else ""
            best_detail = f"유사매칭: 지점={br:.0f}%, 계약자={cn:.0f}%{fb}, 호실={rm:.0f}% → {total:.1f}%"

    if best_score >= threshold and best_idx is not None:
        return MatchResult(
            termination_index=t_idx, settlement_index=best_idx,
            status=MatchStatus.FUZZY, confidence=best_score,
            match_details=best_detail,
            termination_row=t_row, settlement_row=best_row,
        )
    return None


def _cross_verify(result: MatchResult) -> MatchResult:
    verified = []
    t_biz = normalize_business_number(get_col(result.termination_row, "사업자 등록번호"))
    s_biz = normalize_business_number(get_col(result.settlement_row, "사업자등록번호"))
    if t_biz and s_biz:
        verified.append("사업자번호일치" if t_biz == s_biz else "사업자번호불일치")

    t_date = parse_date(get_col(result.termination_row, "계약 시작 날짜"))
    s_date = parse_date(get_col(result.settlement_row, "계약시작일자"))
    if t_date and s_date:
        verified.append("계약시작일일치" if t_date == s_date else "계약시작일불일치")

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
# 결과 → JSON 직렬화
# ============================================================

def results_to_json(results: list[MatchResult]) -> list[dict]:
    out = []
    for r in results:
        out.append({
            "termination_index": r.termination_index,
            "settlement_index": r.settlement_index,
            "status": r.status.value,
            "confidence": round(r.confidence, 1),
            "match_details": r.match_details,
            "verification_passed": r.verification_passed,
            "t_branch": get_col(r.termination_row, "지점명"),
            "t_contractor": extract_name(r.termination_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION),
            "t_room": get_col(r.termination_row, "호실"),
            "t_expiry": get_col(r.termination_row, "계약 만기 날짜"),
            "t_biz_num": get_col(r.termination_row, "사업자 등록번호"),
            "s_branch": get_col(r.settlement_row, "지점명") if r.settlement_row else "",
            "s_contractor": get_col(r.settlement_row, "계약자명") if r.settlement_row else "",
            "s_room": get_col(r.settlement_row, "호실") if r.settlement_row else "",
            "s_biz_num": get_col(r.settlement_row, "사업자등록번호") if r.settlement_row else "",
            "s_note": get_col(r.settlement_row, "계약변경/해지/비고") if r.settlement_row else "",
        })
    return out


def summary(results: list[MatchResult]) -> dict:
    total = len(results)
    exact = sum(1 for r in results if r.status == MatchStatus.EXACT)
    fuzzy_ = sum(1 for r in results if r.status == MatchStatus.FUZZY)
    unmatched = sum(1 for r in results if r.status == MatchStatus.UNMATCHED)
    return {"total": total, "exact": exact, "fuzzy": fuzzy_, "unmatched": unmatched}
