"""
504 타임아웃 전체 파이프라인 진단 스크립트
정산 시트 + 종료 시트 + 매칭 + 변경감지 전체를 테스트합니다.
"""
import json
import os
import time
import urllib.request
import urllib.error

VERCEL_URL = "https://space-ten-beta.vercel.app"
TERM_URL = "https://docs.google.com/spreadsheets/d/1vAcqZOW3YNggesUcjBH2yzdYsenRgdqKLPS0rJ7cISw/edit?gid=921887178#gid=921887178"
TERM_WS = "\uacc4\uc57d \uc885\ub8cc"

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cursor", "debug.log")


def log_entry(hyp, loc, msg, data=None):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    entry = {
        "hypothesisId": hyp, "location": loc, "message": msg,
        "data": data or {}, "timestamp": int(time.time()*1000),
        "runId": "fullpipe-run-1",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        print(f"  [{hyp}] {msg}: {json.dumps(data, ensure_ascii=True) if data else ''}")
    except Exception:
        print(f"  [{hyp}] {msg}: (encoding error)")


def http_get(url, headers=None, timeout=120):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        return {"status": resp.status, "data": data, "elapsed": round(time.time()-t0, 2)}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body[:500]}
        return {"status": e.code, "data": data, "elapsed": round(time.time()-t0, 2)}
    except Exception as e:
        return {"status": -1, "data": {"error": f"{type(e).__name__}: {e}"}, "elapsed": round(time.time()-t0, 2)}


def main():
    print("=" * 60)
    print("Vercel 504 전체 파이프라인 진단")
    print("=" * 60)

    # 전체 파이프라인 진단
    print(f"\n>>> 호출: {VERCEL_URL}/api/diagnose (최대 120초)")
    result = http_get(
        f"{VERCEL_URL}/api/diagnose",
        headers={
            "X-Termination-Url": TERM_URL,
            "X-Termination-Ws": TERM_WS,
        },
        timeout=120,
    )
    print(f"<<< 응답: HTTP {result['status']} ({result['elapsed']}s)")
    log_entry("F", "diagnose:full", "full_pipeline_result", result)

    if result["status"] == 200 and "steps" in result.get("data", {}):
        steps = result["data"]["steps"]
        prev_time = 0
        for s in steps:
            step_time = s.get("elapsed_s", 0)
            delta = round(step_time - prev_time, 3)
            ok = "OK" if s.get("ok") else "FAIL"
            extras = ""
            if "rows" in s:
                extras += f" rows={s['rows']}"
            if "matched" in s:
                extras += f" matched={s['matched']}"
            if "change_count" in s:
                extras += f" changes={s['change_count']}"
            if "payload_bytes" in s:
                extras += f" payload={s['payload_bytes']}B"
            if "error" in s:
                extras += f" error={s['error'][:100]}"
            if "tb" in s:
                extras += f"\n    traceback: {s['tb'][:300]}"

            print(f"  {ok} {s['step']}: {step_time}s (delta={delta}s){extras}")
            log_entry(
                "F" if "matching" not in s.get("step", "") else "F",
                f"diagnose:{s['step']}",
                s["step"],
                {"elapsed_s": step_time, "delta_s": delta, "ok": s.get("ok"), **{k: s[k] for k in s if k not in ("step", "ok", "elapsed_s")}},
            )
            prev_time = step_time

        total = result["data"].get("total_elapsed_s", "?")
        print(f"\n  총 소요시간: {total}s")
        if result["data"].get("warning"):
            print(f"  경고: {result['data']['warning']}")
    else:
        print(f"  비정상 응답: {json.dumps(result.get('data', {}), ensure_ascii=True)[:500]}")

    print(f"\n{'='*60}")
    print(f"로그 저장: {LOG_PATH}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
