"""
504 타임아웃 전체 플로우 진단 스크립트
전체 preview 흐름 (정산 + 종료 + 매칭 + 변경감지)을 단계별 측정합니다.
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
        "runId": "full-flow-diag",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        print(f"  [{hyp}] {msg}: {json.dumps(data, ensure_ascii=True) if data else ''}")
    except Exception:
        print(f"  [{hyp}] {msg}: (encoding error)")


def main():
    print("=" * 60)
    print("Vercel 504 \uc804\uccb4 \ud50c\ub85c\uc6b0 \uc9c4\ub2e8")
    print("=" * 60)

    # 전체 플로우 진단 (종료 시트 포함)
    params = urllib.parse.urlencode({"url": TERM_URL, "ws": TERM_WS})
    endpoint = f"{VERCEL_URL}/api/diagnose?{params}"
    print(f"\n>>> \ud638\ucd9c: {endpoint[:100]}...")
    print(f">>> \ud0c0\uc784\uc544\uc6c3: 120\ucd08")

    t0 = time.time()
    try:
        req = urllib.request.Request(endpoint)
        resp = urllib.request.urlopen(req, timeout=120)
        elapsed = time.time() - t0
        data = json.loads(resp.read())
        print(f"<<< \uc751\ub2f5: HTTP {resp.status} ({elapsed:.2f}s)")

        log_entry("FULL", "diagnose:response", "full_flow_result", {
            "status": resp.status, "data": data, "elapsed": round(elapsed, 2),
        })

        # 각 단계별 타이밍 출력
        if "steps" in data:
            print(f"\n--- \ub2e8\uacc4\ubcc4 \ud0c0\uc774\ubc0d ---")
            prev_time = 0
            for step in data["steps"]:
                step_name = step.get("step", "?")
                step_elapsed = step.get("elapsed_s", 0)
                step_delta = round(step_elapsed - prev_time, 3) if step_elapsed else 0
                ok = step.get("ok", "?")
                extra = ""
                if "rows" in step:
                    extra += f" rows={step['rows']}"
                if "matched" in step:
                    extra += f" matched={step['matched']}"
                if "changes_count" in step:
                    extra += f" changes={step['changes_count']}"
                if "response_size_bytes" in step:
                    extra += f" size={step['response_size_bytes']}B"
                if "error" in step:
                    extra += f" ERROR={step['error'][:80]}"
                if step.get("skipped"):
                    extra = " SKIPPED"

                hyp_map = {
                    "1_imports": "H",
                    "2_gspread_client": "H",
                    "3_settlement_read": "F",
                    "4_termination_read": "F",
                    "5_matching": "G",
                    "6_detect_changes": "G",
                    "7_json_serialize": "I",
                }
                hyp = hyp_map.get(step_name, "?")
                print(f"  [{hyp}] {step_name}: \u0394{step_delta:.3f}s (total {step_elapsed}s) ok={ok}{extra}")
                log_entry(hyp, f"diagnose:{step_name}", f"step_{step_name}", step)
                prev_time = step_elapsed

        if "warning" in data:
            print(f"\n\u26a0\ufe0f WARNING: {data['warning']}")
        print(f"\n\uc804\uccb4 \uc18c\uc694\uc2dc\uac04: {data.get('total_elapsed_s', '?')}s")

    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body = e.read().decode("utf-8", errors="replace")
        print(f"<<< HTTP ERROR {e.code} ({elapsed:.2f}s)")
        print(f"    Body: {body[:200]}")
        log_entry("?", "diagnose:error", "http_error", {
            "code": e.code, "body": body[:500], "elapsed": round(elapsed, 2),
        })
    except Exception as e:
        elapsed = time.time() - t0
        print(f"<<< ERROR: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("?", "diagnose:error", "exception", {
            "type": type(e).__name__, "message": str(e), "elapsed": round(elapsed, 2),
        })

    print(f"\n{'=' * 60}")
    print(f"\ub85c\uadf8 \ud30c\uc77c: {LOG_PATH}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
