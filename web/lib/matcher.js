/**
 * 매칭 엔진 모듈
 *
 * 정산 시트와 계약 종료 시트 간 데이터를 매칭합니다.
 * 1차 정확 매칭 → 2차 퍼지 매칭 → 교차 검증
 */

const fuzz = require("fuzzball");

// ============================================================
// 설정
// ============================================================

/** 매칭 키 (종료 시트 컬럼 → 정산 시트 컬럼) */
const MATCHING_KEYS = {
  지점명: { termination: "지점명", settlement: "지점명" },
  계약자명: { termination: "계약자명", settlement: "계약자명" },
  호실: { termination: "호실", settlement: "호실" },
};

/** 교차 검증 키 */
const VERIFY_KEYS = {
  사업자등록번호: { termination: "사업자 등록번호", settlement: "사업자등록번호" },
  계약시작일자: { termination: "계약 시작 날짜", settlement: "계약시작일자" },
};

/** 동기화 컬럼 */
const SYNC_KEYS = {
  계약만기날짜: { termination: "계약 만기 날짜", settlement: "계약종료일자" },
  결제금액: { termination: "결제 금액", settlement: "결제금액" },
};

/** 이름 폴백 컬럼 (계약자명이 비어있을 때 확인할 컬럼) */
const NAME_FALLBACK_COLUMNS = ["주민 번호", "주민번호", "상호"];

/** 이미 종료 처리된 키워드 */
const TERMINATED_KEYWORDS = ["계약종료", "해지", "종료"];

/** 정규화 시 제거할 문자 */
const REMOVE_CHARS = [" ", "\t", "\n", "-", ".", "(", ")", "\u3000"];

// ============================================================
// 유틸 함수
// ============================================================

/**
 * 행에서 컬럼 값을 유연하게 가져옵니다.
 * 컬럼명의 공백/줄바꿈 차이를 허용합니다.
 */
function getVal(row, colName) {
  if (!row || !colName) return "";
  // 정확한 키
  if (row[colName] !== undefined && row[colName] !== null) {
    return String(row[colName]).trim();
  }
  // 공백/줄바꿈 제거 후 매칭
  const norm = colName.replace(/\s+/g, "");
  for (const key of Object.keys(row)) {
    if (key.replace(/\s+/g, "") === norm) {
      return row[key] !== undefined && row[key] !== null
        ? String(row[key]).trim()
        : "";
    }
  }
  return "";
}

/** 텍스트 정규화 (공백, 특수문자 제거, 소문자) */
function normalizeText(text) {
  if (!text) return "";
  let t = String(text).trim();
  for (const ch of REMOVE_CHARS) {
    t = t.split(ch).join("");
  }
  return t.toLowerCase();
}

/** 주민번호가 아니라 이름인지 판별 */
function isLikelyName(text) {
  if (!text || !String(text).trim()) return false;
  const t = String(text).trim();
  // 숫자+하이픈만 → 주민번호
  if (/^[\d\-\s]+$/.test(t)) return false;
  // 한글 2~5자
  if (/^[가-힣]{2,5}$/.test(t.replace(/\s/g, ""))) return true;
  // 영문 이름
  if (/^[a-zA-Z\s.\-]{2,}$/.test(t) && /[a-zA-Z]/.test(t)) return true;
  // 한글+영문 혼합
  if (/[가-힣]/.test(t) && /[a-zA-Z]/.test(t)) return true;
  return false;
}

/** 이름 정규화 (한글: 공백제거, 영문: 소문자+공백통일) */
function normalizeName(name) {
  if (!name) return "";
  let n = String(name).trim().replace(/\s+/g, " ");
  // 한글만
  if (/^[가-힣\s]+$/.test(n)) return n.replace(/\s/g, "");
  // 영문만
  if (/^[a-zA-Z\s.\-]+$/.test(n)) {
    return n.replace(/[.\-]/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
  }
  // 혼합
  return n.replace(/\s/g, "").toLowerCase();
}

/** 계약자명 추출 (폴백: 주민번호 칸에서 이름 탐색) */
function extractContractorName(row, colName) {
  const name = getVal(row, colName);
  if (name) return name;
  for (const fb of NAME_FALLBACK_COLUMNS) {
    const val = getVal(row, fb);
    if (val && isLikelyName(val)) return val;
  }
  return "";
}

/** 사업자등록번호 정규화 (숫자만) */
function normalizeBizNum(num) {
  if (!num) return "";
  return String(num).replace(/[^0-9]/g, "");
}

/** 날짜 문자열 정규화 (YYYY-MM-DD 형태로 통일) */
function normalizeDate(dateStr) {
  if (!dateStr || !String(dateStr).trim()) return "";
  const d = String(dateStr).trim();
  // 다양한 형식 시도
  const formats = [
    /^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$/,
    /^(\d{2})[-./](\d{1,2})[-./](\d{1,2})$/,
  ];
  for (const fmt of formats) {
    const m = d.match(fmt);
    if (m) {
      let year = m[1].length === 2 ? "20" + m[1] : m[1];
      return `${year}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
    }
  }
  return d;
}

// ============================================================
// 매칭 엔진
// ============================================================

/**
 * 메인 매칭 함수
 * @param {Object[]} settlementRows - 정산 시트 행 배열
 * @param {Object[]} terminationRows - 종료 시트 행 배열
 * @param {number} threshold - 퍼지 매칭 임계값 (기본 80)
 * @returns {Object[]} 매칭 결과 배열
 */
function matchContracts(settlementRows, terminationRows, threshold = 80) {
  const results = [];
  const matchedSettlementIndices = new Set();

  for (let tIdx = 0; tIdx < terminationRows.length; tIdx++) {
    const tRow = terminationRows[tIdx];

    // 1단계: 정확 매칭
    let result = exactMatch(tRow, tIdx, settlementRows, matchedSettlementIndices);

    // 2단계: 퍼지 매칭
    if (!result) {
      result = fuzzyMatch(tRow, tIdx, settlementRows, matchedSettlementIndices, threshold);
    }

    // 매칭 실패
    if (!result) {
      result = {
        terminationIndex: tIdx,
        settlementIndex: null,
        status: "매칭실패",
        confidence: 0,
        details: "매칭되는 정산 건을 찾을 수 없음",
        terminationRow: tRow,
        settlementRow: null,
        verified: false,
      };
    }

    // 3단계: 교차 검증
    if (result.settlementIndex !== null) {
      result = crossVerify(result);
      matchedSettlementIndices.add(result.settlementIndex);
    }

    results.push(result);
  }

  return results;
}

/** 정확 매칭 */
function exactMatch(tRow, tIdx, sRows, matched) {
  const tBranch = normalizeText(getVal(tRow, MATCHING_KEYS.지점명.termination));
  const tName = normalizeName(extractContractorName(tRow, MATCHING_KEYS.계약자명.termination));
  const tRoom = normalizeText(getVal(tRow, MATCHING_KEYS.호실.termination));

  if (!tBranch && !tName) return null;

  const originalName = getVal(tRow, MATCHING_KEYS.계약자명.termination);
  const usedFallback = !originalName && tName;

  for (let sIdx = 0; sIdx < sRows.length; sIdx++) {
    if (matched.has(sIdx)) continue;
    const sRow = sRows[sIdx];

    const sBranch = normalizeText(getVal(sRow, MATCHING_KEYS.지점명.settlement));
    const sName = normalizeName(extractContractorName(sRow, MATCHING_KEYS.계약자명.settlement));
    const sRoom = normalizeText(getVal(sRow, MATCHING_KEYS.호실.settlement));

    const fbNote = usedFallback ? " (주민번호칸에서 이름추출)" : "";

    // 3가지 모두 일치
    if (tBranch === sBranch && tName === sName && tRoom === sRoom) {
      return {
        terminationIndex: tIdx,
        settlementIndex: sIdx,
        status: "정확매칭",
        confidence: 100,
        details: `지점=${tBranch}, 계약자=${tName}${fbNote}, 호실=${tRoom}`,
        terminationRow: tRow,
        settlementRow: sRow,
        verified: false,
      };
    }

    // 지점 + 이름 일치 (호실 비어있음)
    if (tBranch === sBranch && tName === sName && (!tRoom || !sRoom)) {
      return {
        terminationIndex: tIdx,
        settlementIndex: sIdx,
        status: "정확매칭",
        confidence: 90,
        details: `지점=${tBranch}, 계약자=${tName}${fbNote} (호실미확인)`,
        terminationRow: tRow,
        settlementRow: sRow,
        verified: false,
      };
    }
  }
  return null;
}

/** 퍼지 매칭 */
function fuzzyMatch(tRow, tIdx, sRows, matched, threshold) {
  const tBranch = getVal(tRow, MATCHING_KEYS.지점명.termination);
  const tNameRaw = extractContractorName(tRow, MATCHING_KEYS.계약자명.termination);
  const tRoom = getVal(tRow, MATCHING_KEYS.호실.termination);

  if (!tBranch.trim() && !tNameRaw.trim()) return null;

  const originalName = getVal(tRow, MATCHING_KEYS.계약자명.termination);
  const usedFallback = !originalName.trim() && tNameRaw.trim();

  let bestScore = 0;
  let bestIdx = null;
  let bestRow = null;
  let bestDetail = "";

  for (let sIdx = 0; sIdx < sRows.length; sIdx++) {
    if (matched.has(sIdx)) continue;
    const sRow = sRows[sIdx];

    const sBranch = getVal(sRow, MATCHING_KEYS.지점명.settlement);
    const sNameRaw = extractContractorName(sRow, MATCHING_KEYS.계약자명.settlement);
    const sRoom = getVal(sRow, MATCHING_KEYS.호실.settlement);

    // 지점명 유사도
    const branchScore =
      tBranch.trim() && sBranch.trim()
        ? fuzz.ratio(normalizeText(tBranch), normalizeText(sBranch))
        : 0;

    // 계약자명 유사도 (ratio + token_sort_ratio 중 높은 값)
    const tNameN = normalizeName(tNameRaw);
    const sNameN = normalizeName(sNameRaw);
    let nameScore = 0;
    if (tNameN && sNameN) {
      nameScore = Math.max(
        fuzz.ratio(tNameN, sNameN),
        fuzz.token_sort_ratio(tNameN, sNameN)
      );
    }

    // 호실 유사도
    const roomScore =
      tRoom.trim() && sRoom.trim()
        ? fuzz.ratio(normalizeText(tRoom), normalizeText(sRoom))
        : 100;

    // 가중 평균 (지점30% + 이름50% + 호실20%)
    const total = branchScore * 0.3 + nameScore * 0.5 + roomScore * 0.2;

    if (total > bestScore) {
      bestScore = total;
      bestIdx = sIdx;
      bestRow = sRow;
      const fbNote = usedFallback ? " [이름폴백]" : "";
      bestDetail = `지점=${branchScore.toFixed(0)}%, 계약자=${nameScore.toFixed(0)}%${fbNote}, 호실=${roomScore.toFixed(0)}% → ${total.toFixed(1)}%`;
    }
  }

  if (bestScore >= threshold && bestIdx !== null) {
    return {
      terminationIndex: tIdx,
      settlementIndex: bestIdx,
      status: "유사매칭",
      confidence: Math.round(bestScore * 10) / 10,
      details: bestDetail,
      terminationRow: tRow,
      settlementRow: bestRow,
      verified: false,
    };
  }
  return null;
}

/** 교차 검증 */
function crossVerify(result) {
  const checks = [];

  // 사업자등록번호
  const tBiz = normalizeBizNum(getVal(result.terminationRow, VERIFY_KEYS.사업자등록번호.termination));
  const sBiz = normalizeBizNum(getVal(result.settlementRow, VERIFY_KEYS.사업자등록번호.settlement));
  if (tBiz && sBiz) {
    checks.push(tBiz === sBiz ? "사업자번호일치" : "사업자번호불일치");
  }

  // 계약시작일자
  const tDate = normalizeDate(getVal(result.terminationRow, VERIFY_KEYS.계약시작일자.termination));
  const sDate = normalizeDate(getVal(result.settlementRow, VERIFY_KEYS.계약시작일자.settlement));
  if (tDate && sDate) {
    checks.push(tDate === sDate ? "계약시작일일치" : "계약시작일불일치");
  }

  const hasMatch = checks.some((c) => c.includes("일치") && !c.includes("불일치"));
  const hasMismatch = checks.some((c) => c.includes("불일치"));

  if (hasMatch && !hasMismatch) {
    result.verified = true;
    result.details += ` [검증통과: ${checks.join(", ")}]`;
  } else if (hasMismatch) {
    result.verified = false;
    result.details += ` [검증실패: ${checks.join(", ")}]`;
    if (result.status === "정확매칭") {
      result.status = "유사매칭";
      result.details += " → 수동확인 필요";
    }
  } else {
    result.verified = true;
    result.details += " [검증데이터없음]";
  }

  return result;
}

/** 이미 종료 처리된 건인지 확인 */
function isAlreadyTerminated(noteValue) {
  if (!noteValue) return false;
  return TERMINATED_KEYWORDS.some((kw) => noteValue.includes(kw));
}

module.exports = {
  matchContracts,
  getVal,
  isAlreadyTerminated,
  MATCHING_KEYS,
  VERIFY_KEYS,
  SYNC_KEYS,
  TERMINATED_KEYWORDS,
};

