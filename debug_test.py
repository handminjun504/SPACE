"""
v5 매칭 엔진 (날짜 기반) 검증
"""

import json
import time
import urllib.request
import urllib.parse
import os

VERCEL_URL = "https://space-ten-beta.vercel.app"
TERM_URL = "https://docs.google.com/spreadsheets/d/1vAcqZOW3YNggesUcjBH2yzdYsenRgdqKLPS0rJ7cISw/edit"
TERM_WS = "\uacc4\uc57d \uc885\ub8cc"

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cursor", "debug.log")


def log_entry(hyp, loc, msg, data=None):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    entry = {
        "hypothesisId": hyp, "location": loc, "message": msg,
        "data": data or {}, "timestamp": int(time.time() * 1000),
        "runId": "v5-date-match",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def safe_print(s):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("utf-8", errors="replace").decode("ascii", errors="replace"))


def test_version():
    safe_print("\n[1/2] GET /api/preview (version)")
    t0 = time.time()
    try:
        req = urllib.request.Request(f"{VERCEL_URL}/api/preview", method="GET")
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        safe_print(f"  version: {data.get('version', 'N/A')} ({time.time()-t0:.2f}s)")
        return data.get("version")
    except Exception as e:
        safe_print(f"  ERROR: {e}")
        return None


def test_full():
    safe_print("\n[2/2] GET /api/diagnose?mode=full")
    params = urllib.parse.urlencode({"mode": "full", "url": TERM_URL, "ws": TERM_WS})
    endpoint = f"{VERCEL_URL}/api/diagnose?{params}"
    t0 = time.time()
    try:
        req = urllib.request.Request(endpoint)
        resp = urllib.request.urlopen(req, timeout=65)
        elapsed = time.time() - t0
        data = json.loads(resp.read())
        safe_print(f"  HTTP {resp.status} ({elapsed:.2f}s)")

        if "steps" in data:
            prev = 0
            for s in data["steps"]:
                name = s.get("step", "?")
                se = s.get("elapsed_s", 0)
                delta = round(se - prev, 3) if se else 0
                ok = s.get("ok", "?")
                extras = []

                if "rows" in s: extras.append(f"rows={s['rows']}")
                if "matched" in s: extras.append(f"matched={s['matched']}")
                if "exact" in s: extras.append(f"exact={s['exact']}")
                if "fuzzy" in s: extras.append(f"fuzzy={s['fuzzy']}")
                if "phases" in s: extras.append(f"phases={s['phases']}")
                if "match_samples" in s:
                    for ms in s["match_samples"]:
                        safe_print(f"    sample: t[{ms.get('t_idx')}]={ms.get('t_name','?')[:12]} ↔ s[{ms.get('s_idx')}]={ms.get('s_name','?')[:12]} | {ms.get('details','')[:80]}")
                if "changes" in s: extras.append(f"changes={s['changes']}")
                if "resp_bytes" in s: extras.append(f"size={s['resp_bytes']}B")
                # 4b_data_quality: 행 상태 분석
                if "t_total" in s:
                    safe_print(f"    ┌─ 종료시트 {s['t_total']}행 분석 ─────────────")
                    safe_print(f"    │ 빈 행:          {s.get('t_empty_rows', '?')}건")
                    safe_print(f"    │ 지점명 없음:     {s.get('t_no_branch', '?')}건")
                    safe_print(f"    │ 지점명 있음:     {s.get('t_has_branch', '?')}건")
                    safe_print(f"    │ → 정산에 있는 지점: {s.get('t_branch_in_settlement', '?')}건")
                    safe_print(f"    │ → 이름도 있음:     {s.get('t_has_name', '?')}건")
                    safe_print(f"    │ → 매칭 가능:       {s.get('t_matchable', '?')}건 ← 실제 매칭 대상")
                    safe_print(f"    │ 날짜 밀림 보정:   {s.get('t_date_shift_corrected', 0)}건")
                    safe_print(f"    └──────────────────────────────")
                if "branch_comparison" in s and s["branch_comparison"]:
                    safe_print(f"    [공통 지점별 행 수]")
                    for bc in s["branch_comparison"]:
                        safe_print(f"      {bc['branch'][:25]:25s} 종료={bc['t_rows']:4d} 정산={bc['s_rows']:4d}")
                if "t_only_branches" in s and s["t_only_branches"]:
                    safe_print(f"    [매칭 불가 지점 (종료에만 존재)]")
                    for tb in s["t_only_branches"]:
                        safe_print(f"      {tb['branch'][:35]:35s} 종료={tb['t_rows']:4d}건")
                if "matchable_samples" in s and s["matchable_samples"]:
                    safe_print(f"    [매칭 가능 샘플]")
                    for ms in s["matchable_samples"]:
                        safe_print(f"      {ms.get('branch','')[:15]} / {ms.get('name','')[:10]} / {ms.get('room','')}")
                if "error" in s: extras.append(f"ERR={s['error'][:80]}")
                if s.get("tb"): extras.append(f"TB={s['tb'][:80]}")

                ext = " " + " ".join(extras) if extras else ""
                safe_print(f"  {name}: +{delta:.3f}s ok={ok}{ext}")
                log_entry("V5", f"diagnose:{name}", f"step_{name}", s)
                prev = se

        safe_print(f"\n  total: {data.get('total_s', '?')}s")
        log_entry("V5", "diagnose:total", "total", data)

    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body = e.read().decode("utf-8", errors="replace")[:300]
        safe_print(f"  HTTP ERROR {e.code} ({elapsed:.2f}s): {body[:100]}")
        log_entry("V5", "diagnose:error", "error", {"code": e.code, "body": body})
    except Exception as e:
        elapsed = time.time() - t0
        safe_print(f"  ERROR: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("V5", "diagnose:error", "exception", {"type": type(e).__name__, "msg": str(e)})


def main():
    safe_print("=" * 60)
    safe_print("v5 Date-Based Matching Verification")
    safe_print("=" * 60)

    v = test_version()
    test_full()

    safe_print(f"\n{'=' * 60}")


if __name__ == "__main__":
    main()
