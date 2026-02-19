/**
 * 스페이스액스키 계약 종료 동기화 - 프론트엔드 로직
 */

// ============================================================
// 상태
// ============================================================
let password = "";
let previewData = null;
let currentTab = "exact";

// ============================================================
// DOM 요소
// ============================================================
const $ = (sel) => document.querySelector(sel);
const loginScreen = $("#loginScreen");
const mainScreen = $("#mainScreen");
const loginForm = $("#loginForm");
const loginError = $("#loginError");
const logoutBtn = $("#logoutBtn");
const termSheetUrl = $("#termSheetUrl");
const termWorksheet = $("#termWorksheet");
const previewBtn = $("#previewBtn");
const syncBtn = $("#syncBtn");
const resetBtn = $("#resetBtn");
const loadingOverlay = $("#loadingOverlay");
const loadingText = $("#loadingText");

const step1 = $("#step1");
const step2 = $("#step2");
const step3 = $("#step3");

// ============================================================
// API 호출 헬퍼
// ============================================================
async function api(endpoint, body) {
  const res = await fetch(`/api/${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `API 오류 (${res.status})`);
  return data;
}

function showLoading(text = "처리 중...") {
  loadingText.textContent = text;
  loadingOverlay.classList.remove("hidden");
}

function hideLoading() {
  loadingOverlay.classList.add("hidden");
}

// ============================================================
// 로그인
// ============================================================
loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginError.classList.add("hidden");

  const pw = $("#password").value.trim();
  if (!pw) return;

  try {
    showLoading("인증 중...");
    await api("auth", { password: pw });
    password = pw;
    loginScreen.classList.add("hidden");
    mainScreen.classList.remove("hidden");
  } catch (err) {
    loginError.textContent = err.message;
    loginError.classList.remove("hidden");
  } finally {
    hideLoading();
  }
});

logoutBtn.addEventListener("click", () => {
  password = "";
  previewData = null;
  mainScreen.classList.add("hidden");
  loginScreen.classList.remove("hidden");
  $("#password").value = "";
  termSheetUrl.value = "";
  termWorksheet.value = "";
  step2.classList.add("hidden");
  step3.classList.add("hidden");
});

// ============================================================
// 미리보기
// ============================================================
previewBtn.addEventListener("click", async () => {
  const url = termSheetUrl.value.trim();
  if (!url) {
    alert("종료 시트 URL을 입력해주세요.");
    return;
  }

  try {
    showLoading("시트를 읽고 매칭 중...\n(처음에는 30초 정도 걸릴 수 있습니다)");
    const data = await api("preview", {
      password,
      terminationSheetUrl: url,
      terminationWorksheet: termWorksheet.value.trim() || undefined,
    });
    previewData = data;
    renderPreview(data);
    step2.classList.remove("hidden");
    step3.classList.add("hidden");
    step2.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    alert("오류: " + err.message);
  } finally {
    hideLoading();
  }
});

// ============================================================
// 미리보기 렌더링
// ============================================================
function renderPreview(data) {
  const { summary } = data;

  // 요약 박스
  $("#summaryBox").innerHTML = `
    <div class="summary-item summary-total">
      <div class="num">${summary.total}</div>
      <div class="label">전체 종료 건</div>
    </div>
    <div class="summary-item summary-exact">
      <div class="num">${summary.exact}</div>
      <div class="label">정확 매칭</div>
    </div>
    <div class="summary-item summary-fuzzy">
      <div class="num">${summary.fuzzy}</div>
      <div class="label">유사 매칭</div>
    </div>
    <div class="summary-item summary-unmatched">
      <div class="num">${summary.unmatched}</div>
      <div class="label">매칭 실패</div>
    </div>
  `;

  // 카운트 배지
  $("#exactCount").textContent = summary.exact;
  $("#fuzzyCount").textContent = summary.fuzzy;
  $("#unmatchedCount").textContent = summary.unmatched;

  // 기본 탭
  currentTab = "exact";
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  document.querySelector('[data-tab="exact"]').classList.add("active");
  renderTable(data.results);
}

// 탭 클릭
document.addEventListener("click", (e) => {
  if (!e.target.classList.contains("tab")) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  e.target.classList.add("active");
  currentTab = e.target.dataset.tab;
  if (previewData) renderTable(previewData.results);
});

function renderTable(results) {
  const filtered = results.filter((r) => {
    if (currentTab === "exact") return r.status === "정확매칭";
    if (currentTab === "fuzzy") return r.status === "유사매칭";
    return r.status === "매칭실패";
  });

  const tbody = $("#resultBody");
  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:#9ca3af;padding:24px;">데이터 없음</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered
    .map((r) => {
      const statusClass =
        r.status === "정확매칭"
          ? "status-exact"
          : r.status === "유사매칭"
          ? "status-fuzzy"
          : "status-fail";
      const rowClass = r.alreadyTerminated ? "row-already" : "";
      const alreadyMark = r.alreadyTerminated ? " (이미처리)" : "";

      return `<tr class="${rowClass}">
        <td class="${statusClass}">${r.status}${alreadyMark}</td>
        <td>${r.confidence}%</td>
        <td>${esc(r.termination.지점명)}</td>
        <td>${esc(r.termination.계약자명)}</td>
        <td>${esc(r.termination.호실)}</td>
        <td>${esc(r.termination.만기일)}</td>
        <td>${esc(r.settlement.지점명)}</td>
        <td>${esc(r.settlement.계약자명)}</td>
        <td>${esc(r.settlement.호실)}</td>
        <td title="${esc(r.details)}">${truncate(r.details, 40)}</td>
      </tr>`;
    })
    .join("");
}

function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function truncate(str, len) {
  if (!str) return "";
  return str.length > len ? str.substring(0, len) + "…" : str;
}

// ============================================================
// 동기화 실행
// ============================================================
syncBtn.addEventListener("click", async () => {
  if (!previewData) return;

  const matchedCount =
    previewData.summary.exact +
    previewData.results.filter((r) => r.status === "유사매칭" && r.verified).length;

  const confirmed = confirm(
    `${matchedCount}건에 배경색 + 취소선을 적용합니다.\n\n계속하시겠습니까?`
  );
  if (!confirmed) return;

  try {
    showLoading("정산 시트에 서식 적용 중...");
    const url = termSheetUrl.value.trim();
    const data = await api("sync", {
      password,
      terminationSheetUrl: url,
      terminationWorksheet: termWorksheet.value.trim() || undefined,
    });

    renderSyncResult(data);
    step3.classList.remove("hidden");
    step3.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    alert("동기화 오류: " + err.message);
  } finally {
    hideLoading();
  }
});

function renderSyncResult(data) {
  const html = `
    <div class="result-success">
      <div class="check">✓</div>
      <h3>동기화가 완료되었습니다!</h3>
      <p style="color: #6b7280;">정산 시트에 배경색과 취소선이 적용되었습니다.</p>
      <div class="result-stats">
        <div class="stat">
          <div class="val">${data.updated}</div>
          <div class="lbl">업데이트</div>
        </div>
        <div class="stat">
          <div class="val">${data.skipped}</div>
          <div class="lbl">건너뜀 (이미처리)</div>
        </div>
        <div class="stat">
          <div class="val">${data.formatted}</div>
          <div class="lbl">서식 적용</div>
        </div>
      </div>
    </div>
  `;
  $("#syncResult").innerHTML = html;
}

// ============================================================
// 초기화
// ============================================================
resetBtn.addEventListener("click", () => {
  previewData = null;
  termSheetUrl.value = "";
  termWorksheet.value = "";
  step2.classList.add("hidden");
  step3.classList.add("hidden");
  step1.scrollIntoView({ behavior: "smooth", block: "start" });
});

