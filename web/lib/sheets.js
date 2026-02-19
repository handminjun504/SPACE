/**
 * Google Sheets API 연동 모듈
 *
 * 서비스 계정을 사용하여 Google Sheets를 읽고, 쓰고, 서식을 적용합니다.
 */

const { google } = require("googleapis");

/**
 * Google Sheets API 클라이언트를 생성합니다.
 * 환경변수 GOOGLE_CREDENTIALS에서 서비스 계정 JSON을 읽습니다.
 */
function getAuthClient() {
  const credentialsJson = process.env.GOOGLE_CREDENTIALS;
  if (!credentialsJson) {
    throw new Error(
      "GOOGLE_CREDENTIALS 환경변수가 설정되지 않았습니다. " +
        "Vercel 대시보드 → Settings → Environment Variables에서 설정하세요."
    );
  }

  const credentials = JSON.parse(credentialsJson);
  const auth = new google.auth.GoogleAuth({
    credentials,
    scopes: [
      "https://www.googleapis.com/auth/spreadsheets",
      "https://www.googleapis.com/auth/drive.readonly",
    ],
  });
  return auth;
}

/**
 * 구글 시트 ID를 URL에서 추출합니다.
 * @param {string} input - 시트 URL 또는 시트 ID
 * @returns {string} 시트 ID
 */
function extractSheetId(input) {
  if (!input) return "";
  input = input.trim();

  // URL에서 추출: /d/SHEET_ID/
  const match = input.match(/\/d\/([a-zA-Z0-9_-]+)/);
  if (match) return match[1];

  // 이미 ID인 경우
  if (/^[a-zA-Z0-9_-]+$/.test(input)) return input;

  return input;
}

/**
 * 시트 데이터를 읽어서 객체 배열로 반환합니다.
 * @param {string} sheetId - 구글 시트 ID
 * @param {string} worksheetName - 워크시트(탭) 이름
 * @returns {Promise<{headers: string[], rows: Object[], sheetMeta: Object}>}
 */
async function readSheet(sheetId, worksheetName = "Sheet1") {
  const auth = getAuthClient();
  const sheets = google.sheets({ version: "v4", auth });

  // 시트 메타 정보 가져오기 (sheetId 숫자값 필요)
  const metaRes = await sheets.spreadsheets.get({ spreadsheetId: sheetId });
  const sheetMeta = metaRes.data.sheets.find(
    (s) => s.properties.title === worksheetName
  );
  if (!sheetMeta) {
    // 탭 이름에 공백/줄바꿈 차이 허용
    const normalized = worksheetName.replace(/\s+/g, "");
    const found = metaRes.data.sheets.find(
      (s) => s.properties.title.replace(/\s+/g, "") === normalized
    );
    if (!found) {
      const available = metaRes.data.sheets
        .map((s) => s.properties.title)
        .join(", ");
      throw new Error(
        `워크시트 '${worksheetName}'을 찾을 수 없습니다. 사용 가능한 탭: ${available}`
      );
    }
  }

  const actualSheet = sheetMeta || metaRes.data.sheets.find(
    (s) => s.properties.title.replace(/\s+/g, "") === worksheetName.replace(/\s+/g, "")
  );

  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: sheetId,
    range: `'${actualSheet.properties.title}'`,
  });

  const data = res.data.values;
  if (!data || data.length < 2) {
    return { headers: [], rows: [], sheetMeta: actualSheet };
  }

  const headers = data[0].map((h) => h.toString().trim());
  const rows = data.slice(1).map((row, idx) => {
    const obj = { _rowIndex: idx + 1 }; // 1-based (헤더 제외)
    headers.forEach((header, i) => {
      obj[header] = row[i] !== undefined ? row[i].toString().trim() : "";
    });
    return obj;
  });

  return { headers, rows, sheetMeta: actualSheet };
}

/**
 * 지정한 행에 배경색 + 취소선 서식을 적용합니다.
 * @param {string} sheetId - 구글 시트 ID
 * @param {number} numericSheetId - 워크시트 숫자 ID (sheetMeta.properties.sheetId)
 * @param {number[]} rowIndices - 서식 적용할 행 인덱스 배열 (0-based, 헤더 포함)
 * @param {number} totalColumns - 전체 컬럼 수
 * @param {Object} bgColor - 배경색 {red, green, blue} (0~1)
 */
async function applyFormats(
  sheetId,
  numericSheetId,
  rowIndices,
  totalColumns,
  bgColor = { red: 1, green: 0.85, blue: 0.85 }
) {
  if (rowIndices.length === 0) return;

  const auth = getAuthClient();
  const sheets = google.sheets({ version: "v4", auth });

  const requests = rowIndices.map((rowIdx) => ({
    repeatCell: {
      range: {
        sheetId: numericSheetId,
        startRowIndex: rowIdx,
        endRowIndex: rowIdx + 1,
        startColumnIndex: 0,
        endColumnIndex: totalColumns,
      },
      cell: {
        userEnteredFormat: {
          backgroundColor: bgColor,
          textFormat: {
            strikethrough: true,
          },
        },
      },
      fields:
        "userEnteredFormat(backgroundColor,textFormat.strikethrough)",
    },
  }));

  // 100개씩 배치 처리 (API 제한)
  const batchSize = 100;
  for (let i = 0; i < requests.length; i += batchSize) {
    const batch = requests.slice(i, i + batchSize);
    await sheets.spreadsheets.batchUpdate({
      spreadsheetId: sheetId,
      requestBody: { requests: batch },
    });
  }
}

/**
 * 특정 셀에 비고 텍스트를 기록합니다.
 * @param {string} sheetId - 구글 시트 ID
 * @param {string} worksheetName - 워크시트 이름
 * @param {Array<{row: number, col: number, value: string}>} updates - 업데이트 목록
 */
async function updateCells(sheetId, worksheetName, updates) {
  if (updates.length === 0) return;

  const auth = getAuthClient();
  const sheets = google.sheets({ version: "v4", auth });

  const data = updates.map((u) => ({
    range: `'${worksheetName}'!${columnLetter(u.col)}${u.row}`,
    values: [[u.value]],
  }));

  await sheets.spreadsheets.values.batchUpdate({
    spreadsheetId: sheetId,
    requestBody: {
      valueInputOption: "USER_ENTERED",
      data,
    },
  });
}

/**
 * 숫자 컬럼 인덱스를 알파벳으로 변환합니다. (1 → A, 2 → B, ...)
 */
function columnLetter(colNum) {
  let letter = "";
  while (colNum > 0) {
    const mod = (colNum - 1) % 26;
    letter = String.fromCharCode(65 + mod) + letter;
    colNum = Math.floor((colNum - 1) / 26);
  }
  return letter;
}

module.exports = {
  readSheet,
  applyFormats,
  updateCells,
  extractSheetId,
  columnLetter,
};

