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
                if "common_branches" in s: extras.append(f"branches_overlap={s['common_branches']}/{s['t_branches']}")
                if "branch_overlap_pct" in s: extras.append(f"({s['branch_overlap_pct']}%)")
                if "s_has_start_date" in s: extras.append(f"s_dates={s['s_has_start_date']}/{s['s_has_end_date']}")
                # 밀림 보정 전/후 비교
                if "t_raw_start_date" in s:
                    safe_print(f"    [날짜 밀림 보정]")
                    safe_print(f"      보정 전: 시작={s['t_raw_start_date']}, 만기={s['t_raw_end_date']}")
                    safe_print(f"      보정 후: 시작={s.get('t_corrected_start_date','?')}, 만기={s.get('t_corrected_end_date','?')}")
                    safe_print(f"      밀림 보정 건수: {s.get('t_date_shift_corrected', 0)}")
                if "shift_samples" in s and s["shift_samples"]:
                    safe_print(f"      밀림 샘플:")
                    for ss in s["shift_samples"]:
                        safe_print(f"        {ss.get('row_name','?')}: S='{ss.get('S_raw','')}'  T='{ss.get('T_raw','')}' → 시작={ss.get('corrected_start','')}, 만기={ss.get('corrected_end','')}")
                if "t_headers" in s:
                    safe_print(f"    종료시트 헤더: {s['t_headers']}")
                if "sample_common_branches" in s:
                    safe_print(f"    common branches: {s['sample_common_branches']}")
                if "sample_t_only" in s and s['sample_t_only']:
                    safe_print(f"    termination only: {s['sample_t_only']}")
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
