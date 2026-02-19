/**
 * 미리보기 API
 * POST /api/preview
 * Body: { password, terminationSheetUrl, terminationWorksheet? }
 * Returns: { results[], summary }
 */
const { readSheet, extractSheetId } = require("../lib/sheets");
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

    // 결과 변환
    const results = matchResults.map((r) => {
      const tBranch = getVal(r.terminationRow, MATCHING_KEYS.지점명.termination);
      const tName = getVal(r.terminationRow, MATCHING_KEYS.계약자명.termination);
      const tRoom = getVal(r.terminationRow, MATCHING_KEYS.호실.termination);
      const tExpiry = getVal(r.terminationRow, SYNC_KEYS.계약만기날짜.termination);

      let sBranch = "", sName = "", sRoom = "", sNote = "";
      if (r.settlementRow) {
        sBranch = getVal(r.settlementRow, MATCHING_KEYS.지점명.settlement);
        sName = getVal(r.settlementRow, MATCHING_KEYS.계약자명.settlement);
        sRoom = getVal(r.settlementRow, MATCHING_KEYS.호실.settlement);
        sNote = getVal(r.settlementRow, "계약변경/해지/비고");
      }

      const alreadyDone = isAlreadyTerminated(sNote);

      return {
        status: r.status,
        confidence: r.confidence,
        verified: r.verified,
        details: r.details,
        alreadyTerminated: alreadyDone,
        termination: { 지점명: tBranch, 계약자명: tName, 호실: tRoom, 만기일: tExpiry },
        settlement: { 지점명: sBranch, 계약자명: sName, 호실: sRoom, 비고: sNote },
        settlementRowIndex: r.settlementRow ? r.settlementRow._rowIndex : null,
      };
    });

    // 요약
    const total = results.length;
    const exact = results.filter((r) => r.status === "정확매칭").length;
    const fuzzy = results.filter((r) => r.status === "유사매칭").length;
    const unmatched = results.filter((r) => r.status === "매칭실패").length;
    const alreadyDone = results.filter((r) => r.alreadyTerminated).length;

    return res.status(200).json({
      results,
      summary: { total, exact, fuzzy, unmatched, alreadyDone },
      settlementInfo: {
        rows: settlementData.rows.length,
        columns: settlementData.headers.length,
      },
      terminationInfo: {
        rows: terminationData.rows.length,
        columns: terminationData.headers.length,
      },
    });
  } catch (err) {
    console.error("Preview error:", err);
    return res.status(500).json({
      error: err.message || "서버 오류가 발생했습니다.",
    });
  }
};

