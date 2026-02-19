"""
매칭 엔진 모듈 (Vercel 서버리스용) — 초고속 v4

대규모 데이터 대응 (정산 12,831행 × 종료 8,329행):
- 정확 매칭: 사전 인덱스 → O(1) 룩업
- 퍼지 매칭: rapidfuzz.process.extractOne (C 확장) + 지점 그룹핑
  → 수동 루프 대비 10~50배 빠름
- 시간 제한: 15초 (안전 마진 확보)
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
FUZZY_THRESHOLD = 80
FUZZY_TIME_LIMIT = 15  # 퍼지 매칭 최대 시간 (초)
NORMALIZE_REMOVE_CHARS = [" ", "\t", "\n", "-", ".", "(", ")", "\u3000"]
DATE_FORMATS = [
    "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d",
    "%y-%m-%d", "%y.%m.%d", "%y/%m/%d",
]
NAME_FALLBACK_COLUMNS_TERMINATION = ["주민 번호", "상호"]
NAME_FALLBACK_COLUMNS_SETTLEMENT = []
ALREADY_TERMINATED_KEYWORDS = ["계약종료", "해지", "종료"]
TERMINATION_NOTE_TEMPLATE = "계약종료 (만기일: {expiry_date})"

# 내용 변경 감지용 필드 매핑
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
# 사전 정규화 인덱스
# ============================================================

@dataclass
class PreparedRow:
    idx: int
    row: dict
    branch_norm: str
    name_raw: str
    name_norm: str
    room_norm: str


def _prepare_settlement(data: list[dict]) -> tuple[
    dict[tuple, list[PreparedRow]],
    dict[str, list[PreparedRow]],
]:
    exact_index = defaultdict(list)
    branch_index = defaultdict(list)

    for idx, row in enumerate(data):
        pr = PreparedRow(
            idx=idx, row=row,
            branch_norm=normalize_text(get_col(row, "지점명")),
            name_raw=extract_name(row, "계약자명", NAME_FALLBACK_COLUMNS_SETTLEMENT),
            name_norm="",
            room_norm=normalize_text(get_col(row, "호실")),
        )
        pr.name_norm = normalize_name(pr.name_raw)
        exact_index[(pr.branch_norm, pr.name_norm)].append(pr)
        branch_index[pr.branch_norm].append(pr)

    return exact_index, branch_index


def _prepare_termination(data: list[dict]) -> list[PreparedRow]:
    result = []
    for idx, row in enumerate(data):
        pr = PreparedRow(
            idx=idx, row=row,
            branch_norm=normalize_text(get_col(row, "지점명")),
            name_raw=extract_name(row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION),
            name_norm="",
            room_norm=normalize_text(get_col(row, "호실")),
        )
        pr.name_norm = normalize_name(pr.name_raw)
        result.append(pr)
    return result


# ============================================================
# rapidfuzz.process 기반 배치 인덱스
# ============================================================

def _build_fuzzy_index(branch_index: dict[str, list[PreparedRow]]) -> dict[str, tuple[list[str], list[PreparedRow]]]:
    """
    지점별로 (이름 리스트, PreparedRow 리스트) 쌍을 만듦.
    rapidfuzz.process.extractOne은 choices 리스트를 받으므로
    인덱스를 통해 원본 행을 역추적할 수 있어야 함.
    """
    fuzzy_idx = {}
    for branch_norm, rows in branch_index.items():
        if not branch_norm:
            continue
        names = []
        prs = []
        for pr in rows:
            names.append(pr.name_norm)
            prs.append(pr)
        fuzzy_idx[branch_norm] = (names, prs)
    return fuzzy_idx


# ============================================================
# 매칭 엔진 v4
# ============================================================

def run_matching(
    settlement_data: list[dict],
    termination_data: list[dict],
    threshold: int = FUZZY_THRESHOLD,
) -> list[MatchResult]:
    t0 = time.time()

    exact_index, branch_index = _prepare_settlement(settlement_data)
    t_prepared = _prepare_termination(termination_data)
    fuzzy_idx = _build_fuzzy_index(branch_index)

    results = []
    matched_indices: set[int] = set()

    # Phase 1: 정확 매칭
    unmatched_t = []
    for tp in t_prepared:
        result = _exact_match(tp, exact_index, matched_indices)
        if result is not None:
            result = _cross_verify(result)
            matched_indices.add(result.settlement_index)
            results.append(result)
        else:
            unmatched_t.append(tp)

    # Phase 2: 퍼지 매칭 (rapidfuzz.process.extractOne 사용)
    fuzzy_start = time.time()
    fuzzy_timeout = False

    for tp in unmatched_t:
        if time.time() - fuzzy_start > FUZZY_TIME_LIMIT:
            fuzzy_timeout = True
            idx_pos = unmatched_t.index(tp)
            for remaining in unmatched_t[idx_pos:]:
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
            result = _cross_verify(result)
            matched_indices.add(result.settlement_index)
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
    return results


def _exact_match(tp: PreparedRow, exact_index, matched) -> Optional[MatchResult]:
    if not tp.branch_norm and not tp.name_norm:
        return None

    original = get_col(tp.row, "계약자명")
    used_fb = (not original.strip()) and tp.name_raw.strip()
    fb_note = " (주민번호칸 이름)" if used_fb else ""

    candidates = exact_index.get((tp.branch_norm, tp.name_norm), [])

    for sp in candidates:
        if sp.idx not in matched and tp.room_norm == sp.room_norm:
            return MatchResult(
                termination_index=tp.idx, settlement_index=sp.idx,
                status=MatchStatus.EXACT, confidence=100.0,
                match_details=f"정확매칭: {tp.branch_norm}/{tp.name_raw}{fb_note}/{tp.room_norm}",
                termination_row=tp.row, settlement_row=sp.row,
            )

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


def _fuzzy_match_fast(
    tp: PreparedRow, fuzzy_idx, matched, threshold,
) -> Optional[MatchResult]:
    """
    rapidfuzz.process.extractOne 기반 초고속 퍼지 매칭
    C 확장으로 수백 개 문자열을 한 번에 비교 → 수동 루프 대비 10~50x 빠름
    """
    if not tp.branch_norm or not tp.name_norm:
        return None

    entry = fuzzy_idx.get(tp.branch_norm)
    if not entry:
        return None

    names, prs = entry

    # rapidfuzz.process.extractOne: 최적의 매칭을 C 레벨에서 찾음
    # score_cutoff으로 미리 걸러냄
    # 이름 매칭 최소 점수: threshold에서 지점(30%)+호실(최대20%) 제외 → 이름만 60% 이상
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
        # 최고 매칭이 이미 사용된 경우, 차선책 검색
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

    total = 100 * 0.3 + name_score * 0.5 + rm * 0.2

    if total < threshold:
        return None

    original = get_col(tp.row, "계약자명")
    fb = " [이름폴백]" if (not original.strip()) and tp.name_raw.strip() else ""
    detail = f"유사매칭: 지점=100%, 계약자={name_score:.0f}%{fb}, 호실={rm:.0f}% → {total:.1f}%"

    return MatchResult(
        termination_index=tp.idx, settlement_index=sp.idx,
        status=MatchStatus.FUZZY, confidence=total,
        match_details=detail,
        termination_row=tp.row, settlement_row=sp.row,
    )


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
                branch=get_col(r.termination_row, "지점명"),
                contractor=extract_name(r.termination_row, "계약자명", NAME_FALLBACK_COLUMNS_TERMINATION),
                room=get_col(r.termination_row, "호실"),
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
    return [{
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
    } for r in results]


def summary(results: list[MatchResult]) -> dict:
    total = len(results)
    exact = sum(1 for r in results if r.status == MatchStatus.EXACT)
    fuzzy_ = sum(1 for r in results if r.status == MatchStatus.FUZZY)
    unmatched = sum(1 for r in results if r.status == MatchStatus.UNMATCHED)
    return {"total": total, "exact": exact, "fuzzy": fuzzy_, "unmatched": unmatched}
