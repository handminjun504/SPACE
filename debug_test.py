"""
504 타임아웃 단계별 격리 진단
3번의 독립 API 호출로 어떤 단계가 느린지 측정합니다.
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
        "runId": "isolated-diag",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call_diagnose(mode, params_extra=None):
    params = {"mode": mode}
    if params_extra:
        params.update(params_extra)
    qs = urllib.parse.urlencode(params)
    endpoint = f"{VERCEL_URL}/api/diagnose?{qs}"

    label = {
        "settlement": "[F] \uc815\uc0b0 \uc2dc\ud2b8\ub9cc",
        "termination": "[F] \uc885\ub8cc \uc2dc\ud2b8\ub9cc",
        "full": "[G] \uc804\uccb4 \ud50c\ub85c\uc6b0",
    }.get(mode, mode)

    print(f"\n--- {label} ---")
    print(f"  URL: {endpoint[:80]}...")

    t0 = time.time()
    try:
        req = urllib.request.Request(endpoint)
        resp = urllib.request.urlopen(req, timeout=65)
        elapsed = time.time() - t0
        data = json.loads(resp.read())

        print(f"  HTTP {resp.status} ({elapsed:.2f}s)")
        for step in data.get("steps", []):
            name = step.get("step", "?")
            ok = step.get("ok", "?")
            se = step.get("elapsed_s", 0)
            extra_parts = []
            if "rows" in step:
                extra_parts.append(f"rows={step['rows']}")
            if "matched" in step:
                extra_parts.append(f"matched={step['matched']}")
            if "changes" in step:
                extra_parts.append(f"changes={step['changes']}")
            if "resp_bytes" in step:
                extra_parts.append(f"size={step['resp_bytes']}B")
            if "error" in step:
                extra_parts.append(f"ERR={step['error'][:60]}")
            extra = " " + " ".join(extra_parts) if extra_parts else ""
            print(f"    {name}: ok={ok} {se}s{extra}")

        log_entry(mode[0].upper(), f"diagnose:{mode}", f"{mode}_result", {
            "status": resp.status, "data": data, "elapsed": round(elapsed, 2),
        })
        return data

    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  HTTP ERROR {e.code} ({elapsed:.2f}s): {body[:80]}")
        log_entry("!", f"diagnose:{mode}", f"{mode}_error", {
            "code": e.code, "body": body, "elapsed": round(elapsed, 2),
        })
        return None

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  EXCEPTION: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("!", f"diagnose:{mode}", f"{mode}_exception", {
            "type": type(e).__name__, "msg": str(e), "elapsed": round(elapsed, 2),
        })
        return None


def main():
    print("=" * 60)
    print("Vercel 504 \ub2e8\uacc4\ubcc4 \uaca9\ub9ac \uc9c4\ub2e8")
    print("=" * 60)

    # Step A: 정산 시트만 (이전에 5초 성공)
    call_diagnose("settlement")

    # Step B: 종료 시트만 (미검증 영역!)
    call_diagnose("termination", {"url": TERM_URL, "ws": TERM_WS})

    # Step C: 전체 플로우 (정산+종료+매칭+변경감지)
    call_diagnose("full", {"url": TERM_URL, "ws": TERM_WS})

    print(f"\n{'=' * 60}")
    print(f"\ub85c\uadf8: {LOG_PATH}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
