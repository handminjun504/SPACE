"""
Preview 엔드포인트 직접 테스트 (POST)
diagnose(GET)은 36초 성공, preview(POST)는 504 - 차이점 확인
"""

import json
import time
import urllib.request
import urllib.parse
import os

VERCEL_URL = "https://space-ten-beta.vercel.app"
TERM_URL = "https://docs.google.com/spreadsheets/d/1vAcqZOW3YNggesUcjBH2yzdYsenRgdqKLPS0rJ7cISw/edit"
TERM_WS = "\uacc4\uc57d \uc885\ub8cc"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cursor", "debug.log")


def log_entry(hyp, loc, msg, data=None):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    entry = {
        "hypothesisId": hyp, "location": loc, "message": msg,
        "data": data or {}, "timestamp": int(time.time() * 1000),
        "runId": "preview-direct-test",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_endpoint(label, method, url, headers=None, body=None, timeout=65):
    print(f"\n--- {label} ---")
    print(f"  {method} {url[:80]}...")
    t0 = time.time()
    try:
        if body:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        else:
            req = urllib.request.Request(url, headers=headers or {}, method=method)
        resp = urllib.request.urlopen(req, timeout=timeout)
        elapsed = time.time() - t0
        resp_body = resp.read()
        resp_data = json.loads(resp_body)
        print(f"  HTTP {resp.status} ({elapsed:.2f}s) size={len(resp_body)}B")

        # 요약 출력
        if "summary" in resp_data:
            print(f"  summary: {json.dumps(resp_data['summary'], ensure_ascii=False)}")
        if "elapsed_seconds" in resp_data:
            print(f"  server_elapsed: {resp_data['elapsed_seconds']}s")
        if "steps" in resp_data:
            for s in resp_data["steps"]:
                name = s.get("step", "?")
                ok = s.get("ok", "?")
                se = s.get("elapsed_s", "?")
                extra = ""
                if "rows" in s: extra += f" rows={s['rows']}"
                if "matched" in s: extra += f" matched={s['matched']}"
                if "error" in s: extra += f" ERR={s['error'][:60]}"
                print(f"    {name}: ok={ok} {se}s{extra}")

        log_entry("P", f"test:{label}", "success", {
            "status": resp.status, "elapsed": round(elapsed, 2),
            "size": len(resp_body),
            "data_keys": list(resp_data.keys()),
            "summary": resp_data.get("summary"),
            "server_elapsed": resp_data.get("elapsed_seconds"),
        })
        return resp_data

    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        print(f"  HTTP ERROR {e.code} ({elapsed:.2f}s)")
        print(f"  Body: {body_text[:120]}")
        log_entry("P", f"test:{label}", "http_error", {
            "code": e.code, "elapsed": round(elapsed, 2), "body": body_text,
        })
        return None

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  EXCEPTION: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("P", f"test:{label}", "exception", {
            "type": type(e).__name__, "msg": str(e), "elapsed": round(elapsed, 2),
        })
        return None


def main():
    global APP_PASSWORD
    print("=" * 60)
    print("Preview vs Diagnose \uc9c1\uc811 \ube44\uad50 \ud14c\uc2a4\ud2b8")
    print("=" * 60)

    # 비밀번호 입력
    if not APP_PASSWORD:
        APP_PASSWORD = input("\ube44\ubc00\ubc88\ud638 \uc785\ub825 (APP_PASSWORD): ").strip()
        if not APP_PASSWORD:
            print("ERROR: \ube44\ubc00\ubc88\ud638\uac00 \ud544\uc694\ud569\ub2c8\ub2e4.")
            return

    # Test 1: Auth 확인
    test_endpoint(
        "1. Auth",
        "POST",
        f"{VERCEL_URL}/api/auth",
        headers={"Content-Type": "application/json", "X-Password": APP_PASSWORD},
        body={"password": APP_PASSWORD},
        timeout=30,
    )

    # Test 2: Diagnose full (GET) - 이전에 36초 성공
    params = urllib.parse.urlencode({"mode": "full", "url": TERM_URL, "ws": TERM_WS})
    test_endpoint(
        "2. Diagnose-full (GET)",
        "GET",
        f"{VERCEL_URL}/api/diagnose?{params}",
        timeout=65,
    )

    # Test 3: Preview (POST) - 504가 발생하는 엔드포인트
    test_endpoint(
        "3. Preview (POST)",
        "POST",
        f"{VERCEL_URL}/api/preview",
        headers={"Content-Type": "application/json", "X-Password": APP_PASSWORD},
        body={"termination_url": TERM_URL, "worksheet_name": TERM_WS},
        timeout=65,
    )

    print(f"\n{'=' * 60}")
    print(f"\ub85c\uadf8: {LOG_PATH}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
