/**
 * 동기화 실행 API
 * POST /api/sync
 * Body: { password, terminationSheetUrl, terminationWorksheet? }
 * Returns: { updated, skipped, formatted, summary }
 *
 * 정산 시트에서 종료 건에 배경색 + 취소선을 적용하고
 * 비고 컬럼에 "계약종료 (만기일: YYYY-MM-DD)"를 기록합니다.
 */
const { readSheet, extractSheetId, applyFormats, updateCells } = require("../lib/sheets");
const { matchContracts, getVal, isAlreadyTerminated, MATCHING_KEYS, SYNC_KEYS } = require("../lib/matcher");

module.exports = async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();

  if (req.method !== "POST") {
    return res.status(405).json({ error: "POST만 허용됩니다." });
  }

  try {
    // 인증 체크
    const { password, terminationSheetUrl, terminationWorksheet } = req.body || {};
    if (password !== process.env.APP_PASSWORD) {
      return res.status(401).json({ error: "인증 실패" });
    }

    if (!terminationSheetUrl) {
      return res.status(400).json({ error: "종료 시트 URL을 입력해주세요." });
    }

    // 시트 읽기
    const settlementSheetId = process.env.SETTLEMENT_SHEET_ID;
    const settlementWorksheetName = process.env.SETTLEMENT_WORKSHEET_NAME || "Sheet1";
    const termSheetId = extractSheetId(terminationSheetUrl);
    const termWorksheetName = terminationWorksheet || process.env.TERMINATION_WORKSHEET_NAME || "Sheet1";

    const [settlementData, terminationData] = await Promise.all([
      readSheet(settlementSheetId, settlementWorksheetName),
      readSheet(termSheetId, termWorksheetName),
    ]);

    // 매칭 실행
    const threshold = parseInt(process.env.FUZZY_THRESHOLD || "80", 10);
    const matchResults = matchContracts(
      settlementData.rows,
      terminationData.rows,
      threshold
    );

    // 업데이트 대상 필터링 (정확매칭 또는 검증통과한 유사매칭)
    const updatable = matchResults.filter(
      (r) =>
        r.status === "정확매칭" ||
        (r.status === "유사매칭" && r.verified)
    );

    // 비고 컬럼 인덱스 찾기
    const noteColName = "계약변경/해지/비고";
    const noteColIdx = settlementData.headers.findIndex(
      (h) => h.replace(/\s+/g, "") === noteColName.replace(/\s+/g, "")
    );

    const rowsToFormat = []; // 배경색+취소선 적용할 행
    const cellUpdates = []; // 비고 텍스트 업데이트
    let updated = 0;
    let skipped = 0;
    const updateLog = [];

    for (const r of updatable) {
      const sRow = r.settlementRow;
      const existingNote = getVal(sRow, noteColName);

      // 이미 종료 처리된 건 건너뛰기
      if (isAlreadyTerminated(existingNote)) {
        skipped++;
        updateLog.push({
          status: "건너뜀",
          지점명: getVal(r.terminationRow, MATCHING_KEYS.지점명.termination),
          계약자명: getVal(r.terminationRow, MATCHING_KEYS.계약자명.termination),
          사유: "이미 종료 처리됨",
        });
        continue;
      }

      const expiry = getVal(r.terminationRow, SYNC_KEYS.계약만기날짜.termination);
      const newNote = `계약종료 (만기일: ${expiry || "미확인"})`;
      const finalNote = existingNote ? `${existingNote} | ${newNote}` : newNote;

      // 행 인덱스: _rowIndex는 1-based (헤더 제외) → 시트에서는 +1 (헤더 포함)
      const sheetRowIdx = sRow._rowIndex + 1; // 0-based for formatting API
      const sheetRowNum = sRow._rowIndex + 1; // 1-based for values API

      // 서식 적용할 행 등록
      rowsToFormat.push(sheetRowIdx);

      // 비고 컬럼 업데이트 등록
      if (noteColIdx >= 0) {
        cellUpdates.push({
          row: sheetRowNum,
          col: noteColIdx + 1, // 1-based
          value: finalNote,
        });
      }

      updated++;
      updateLog.push({
        status: "업데이트",
        지점명: getVal(r.terminationRow, MATCHING_KEYS.지점명.termination),
        계약자명: getVal(r.terminationRow, MATCHING_KEYS.계약자명.termination),
        호실: getVal(r.terminationRow, MATCHING_KEYS.호실.termination),
        만기일: expiry,
        비고: finalNote,
      });
    }

    // 실제 시트 업데이트 실행
    if (rowsToFormat.length > 0) {
      const numericSheetId = settlementData.sheetMeta.properties.sheetId;
      // 배경색(연한 빨간) + 취소선 적용
      await applyFormats(
        settlementSheetId,
        numericSheetId,
        rowsToFormat,
        settlementData.headers.length,
        { red: 1, green: 0.82, blue: 0.82 } // #FFD1D1 연한 빨강
      );
    }

    if (cellUpdates.length > 0) {
      await updateCells(settlementSheetId, settlementWorksheetName, cellUpdates);
    }

    // 전체 요약
    const total = matchResults.length;
    const exact = matchResults.filter((r) => r.status === "정확매칭").length;
    const fuzzy = matchResults.filter((r) => r.status === "유사매칭").length;
    const unmatched = matchResults.filter((r) => r.status === "매칭실패").length;

    return res.status(200).json({
      success: true,
      updated,
      skipped,
      formatted: rowsToFormat.length,
      updateLog,
      summary: { total, exact, fuzzy, unmatched },
    });
  } catch (err) {
    console.error("Sync error:", err);
    return res.status(500).json({
      error: err.message || "동기화 중 오류가 발생했습니다.",
    });
  }
};

