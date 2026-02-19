"""
매칭 엔진 모듈 (Vercel 서버리스용) — 초고속 버전

12,831행 정산 시트 대응:
- 정확 매칭: (지점명, 계약자명) 인덱스 → O(1)
- 퍼지 매칭: 지점명 그룹핑 → 같은 지점 내에서만 비교 (20배↑)
"""

import re
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from rapidfuzz import fuzz

# region agent log
import os as _os
_DEBUG_LOG = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), ".cursor", "debug.log")
def _dlog(hyp, loc, msg, data=None):
    try:
        entry = {"hypothesisId": hyp, "location": loc, "message": msg, "data": data or {}, "timestamp": int(time.time()*1000), "runId": "matcher-run"}
        _os.makedirs(_os.path.dirname(_DEBUG_LOG), exist_ok=True)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
# endregion

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

# ============================================================
# 내용 변경 감지용 필드 매핑
# (정산 시트 컬럼명, 종료 시트 컬럼명, 표시명, 비교 타입)
# ============================================================
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
# 매칭 엔진 (지점명 그룹핑 최적화)
# ============================================================

def run_matching(
    settlement_data: list[dict],
    termination_data: list[dict],
    threshold: int = FUZZY_THRESHOLD,
) -> list[MatchResult]:
    t0 = time.time()

    results = []
    matched_indices: set[int] = set()

    # === 사전 인덱싱 ===
    # 1) 정확 매칭용: (branch, name) → [(idx, dict, room, name_raw)]
    exact_index = defaultdict(list)
    # 2) 퍼지 매칭용: branch_norm → [(idx, dict, branch_raw, name_raw, room_raw)]
    branch_groups = defaultdict(list)
    unique_branches = set()

    for s_idx, s_dict in enumerate(settlement_data):
        s_branch_norm = normalize_text(get_col(s_dict, "지점명"))
        s_name_raw = extract_name(s_dict, "계약자명", NAME_FALLBACK_COLUMNS_SETTLEMENT)
        s_name_norm = normalize_name(s_name_raw)
        s_room_norm = normalize_text(get_col(s_dict, "호실"))

        exact_index[(s_branch_norm, s_name_norm)].append(
            (s_idx, s_dict, s_room_norm, s_name_raw)
        )
        branch_groups[s_branch_norm].append(
            (s_idx, s_dict, s_name_raw, get_col(s_dict, "호실"))
        )
        unique_branches.add(s_branch_norm)

    # region agent log
    _dlog("F", "matcher:index", "indexing_done", {
        "settlement_rows": len(settlement_data),
        "termination_rows": len(termination_data),
        "unique_branches": len(unique_branches),
        "index_time_s": round(time.time()-t0, 3),
    })
    # endregion

    t1 = time.time()
    exact_count = 0
    fuzzy_count = 0
    fuzzy_comparisons = 0

    for t_idx, t_dict in enumerate(termination_data):
        # 정확 매칭 (O(1) 룩업)
        result = _exact_match(t_idx, t_dict, exact_index, matched_indices)
        if result is not None:
            exact_count += 1
        else:
            # 퍼지 매칭 (같은 지점 내에서만 비교)
            result, comps = _fuzzy_match_grouped(
                t_idx, t_dict, branch_groups, unique_branches,
                matched_indices, threshold,
            )
            fuzzy_comparisons += comps
            if result is not None:
                fuzzy_count += 1

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

    # region agent log
    _dlog("F", "matcher:matching", "matching_done", {
        "exact_matches": exact_count,
        "fuzzy_matches": fuzzy_count,
        "fuzzy_comparisons": fuzzy_comparisons,
        "matching_time_s": round(time.time()-t1, 3),
        "total_time_s": round(time.time()-t0, 3),
    })
    # endregion

    return results


def _exact_match(t_idx, t_row, exact_index, matched) -> Optional[MatchResult]:
    t_branch = normalize_text(get_col(t_row, "지점명"))
    t_room = normalize_text(get_col(t_row, "호실"))
    t_name_raw = extract_name(t_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION)
    t_name = normalize_name(t_name_raw)

    if not t_branch and not t_name:
        return None

    original = get_col(t_row, "계약자명")
    used_fb = (not original.strip()) and t_name_raw.strip()
    fb_note = " (주민번호칸 이름)" if used_fb else ""

    candidates = exact_index.get((t_branch, t_name), [])

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


def _fuzzy_match_grouped(
    t_idx, t_row, branch_groups, unique_branches,
    matched, threshold,
) -> tuple[Optional[MatchResult], int]:
    """
    지점명 기반 그룹핑 퍼지 매칭:
    1) 종료 시트의 지점명과 유사한 정산 지점만 찾음
    2) 해당 지점 그룹 내에서만 계약자명/호실 비교
    → 12,831행 전체 대신 ~600행만 비교 (20배 빠름)
    """
    t_branch = get_col(t_row, "지점명")
    t_name_raw = extract_name(t_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION)
    t_room = get_col(t_row, "호실")

    if not t_branch.strip() and not t_name_raw.strip():
        return None, 0

    original = get_col(t_row, "계약자명")
    used_fb = (not original.strip()) and t_name_raw.strip()

    t_branch_norm = normalize_text(t_branch)
    t_name_norm = normalize_name(t_name_raw)
    t_room_norm = normalize_text(t_room)

    # Step 1: 유사한 지점명 찾기 (전체 지점 수는 적으므로 빠름)
    matching_branches = []
    for branch_norm in unique_branches:
        if not branch_norm:
            continue
        br_score = fuzz.ratio(t_branch_norm, branch_norm) if t_branch_norm else 0
        if br_score >= 50:
            matching_branches.append((branch_norm, br_score))

    if not matching_branches:
        return None, 0

    # Step 2: 해당 지점 그룹 내에서만 계약자명/호실 비교
    best_score, best_idx, best_row, best_detail = 0.0, None, None, ""
    total_comparisons = 0

    for branch_norm, br_score in matching_branches:
        for s_idx, s_dict, s_name_raw, s_room in branch_groups[branch_norm]:
            if s_idx in matched:
                continue

            total_comparisons += 1
            s_name_norm = normalize_name(s_name_raw)
            s_room_norm = normalize_text(s_room)

            cn = max(
                fuzz.ratio(t_name_norm, s_name_norm),
                fuzz.token_sort_ratio(t_name_norm, s_name_norm),
            ) if t_name_norm and s_name_norm else 0
            rm = fuzz.ratio(t_room_norm, s_room_norm) if t_room_norm and s_room_norm else 100

            total = br_score * 0.3 + cn * 0.5 + rm * 0.2
            if total > best_score:
                best_score, best_idx, best_row = total, s_idx, s_dict
                fb = " [이름폴백]" if used_fb else ""
                best_detail = f"유사매칭: 지점={br_score:.0f}%, 계약자={cn:.0f}%{fb}, 호실={rm:.0f}% → {total:.1f}%"

    if best_score >= threshold and best_idx is not None:
        return MatchResult(
            termination_index=t_idx, settlement_index=best_idx,
            status=MatchStatus.FUZZY, confidence=best_score,
            match_details=best_detail,
            termination_row=t_row, settlement_row=best_row,
        ), total_comparisons
    return None, total_comparisons


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
# 내용 변경 감지 (두 시트 간 필드 비교)
# ============================================================

def _normalize_number(val: str) -> str:
    """숫자 비교용 정규화: 콤마/공백 제거, 소수점 이하 0 제거"""
    if not val or not val.strip():
        return ""
    cleaned = re.sub(r"[^\d.\-]", "", val.strip())
    if not cleaned:
        return ""
    try:
        num = float(cleaned)
        if num == int(num):
            return str(int(num))
        return str(num)
    except ValueError:
        return cleaned


def _normalize_date_str(val: str) -> str:
    """날짜 비교용 정규화: 다양한 형식 → YYYY-MM-DD"""
    dt = parse_date(val)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return val.strip() if val else ""


def _normalize_for_compare(val: str, compare_type: str) -> str:
    """비교 타입에 따라 값을 정규화"""
    if compare_type == "number":
        return _normalize_number(val)
    elif compare_type == "date":
        return _normalize_date_str(val)
    elif compare_type == "biz_number":
        return normalize_business_number(val)
    else:  # text
        return normalize_text(val)


@dataclass
class FieldDiff:
    """필드 하나의 변경 내역"""
    field_name: str           # 표시용 이름 (예: "결제금액")
    settlement_value: str     # 정산 시트 원본값
    termination_value: str    # 종료 시트 원본값


@dataclass
class ChangeResult:
    """매칭된 한 건의 내용 변경 감지 결과"""
    settlement_index: int
    termination_index: int
    branch: str
    contractor: str
    room: str
    diffs: list[FieldDiff]
    settlement_row: dict = field(default_factory=dict)
    termination_row: dict = field(default_factory=dict)


def detect_changes(
    match_results: list[MatchResult],
) -> list[ChangeResult]:
    """
    매칭된 건들에서 두 시트 간 내용 차이를 감지합니다.
    결제금액, 계약시작일, 계약종료일, 사업자등록번호, 호실을 비교합니다.
    """
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

            # 둘 다 비어있으면 차이 아님
            if not s_norm and not t_norm:
                continue

            # 한쪽만 비어있거나 값이 다르면 변경
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
                branch=get_col(r.termination_row, "지점명"),
                contractor=extract_name(r.termination_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION),
                room=get_col(r.termination_row, "호실"),
                diffs=diffs,
                settlement_row=r.settlement_row,
                termination_row=r.termination_row,
            ))

    return changes


def changes_to_json(changes: list[ChangeResult]) -> list[dict]:
    """ChangeResult 리스트를 JSON 직렬화"""
    out = []
    for c in changes:
        out.append({
            "settlement_index": c.settlement_index,
            "termination_index": c.termination_index,
            "branch": c.branch,
            "contractor": c.contractor,
            "room": c.room,
            "diffs": [
                {
                    "field": d.field_name,
                    "settlement": d.settlement_value,
                    "termination": d.termination_value,
                }
                for d in c.diffs
            ],
        })
    return out


def changes_summary(changes: list[ChangeResult]) -> dict:
    """변경 감지 요약"""
    field_counts = defaultdict(int)
    for c in changes:
        for d in c.diffs:
            field_counts[d.field_name] += 1
    return {
        "total_changed": len(changes),
        "by_field": dict(field_counts),
    }


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
