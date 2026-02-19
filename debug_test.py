"""
v4 preview 배포 상태 및 성능 검증
1단계: GET /api/preview → 배포된 버전 확인
2단계: POST /api/preview (잘못된 비번) → 401 응답에 _v 필드 확인
3단계: GET /api/diagnose?mode=full → 전체 파이프라인 성능 확인
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
        "runId": "v4-preview-verify",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def safe_print(s):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("utf-8", errors="replace").decode("ascii", errors="replace"))


def test_preview_version():
    """GET /api/preview → 배포된 코드 버전 확인"""
    safe_print("\n[1/3] GET /api/preview (버전 확인)")
    endpoint = f"{VERCEL_URL}/api/preview"
    t0 = time.time()
    try:
        req = urllib.request.Request(endpoint, method="GET")
        resp = urllib.request.urlopen(req, timeout=15)
        elapsed = time.time() - t0
        data = json.loads(resp.read())
        safe_print(f"  HTTP {resp.status} ({elapsed:.2f}s)")
        safe_print(f"  version: {data.get('version', 'NOT FOUND')}")
        safe_print(f"  import_duration: {data.get('import_duration_s', '?')}s")
        log_entry("K", "preview_get", "version_check", {"status": resp.status, "data": data, "elapsed": round(elapsed, 2)})
        return data.get("version")
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body = e.read().decode("utf-8", errors="replace")[:300]
        safe_print(f"  HTTP ERROR {e.code} ({elapsed:.2f}s): {body[:100]}")
        log_entry("K", "preview_get", "error", {"code": e.code, "body": body, "elapsed": round(elapsed, 2)})
        return None
    except Exception as e:
        elapsed = time.time() - t0
        safe_print(f"  ERROR: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("K", "preview_get", "exception", {"type": type(e).__name__, "msg": str(e), "elapsed": round(elapsed, 2)})
        return None


def test_preview_auth_fail():
    """POST /api/preview with wrong password → 401에 _v 필드 확인"""
    safe_print("\n[2/3] POST /api/preview (잘못된 비번 → 401 확인)")
    endpoint = f"{VERCEL_URL}/api/preview"
    t0 = time.time()
    try:
        body = json.dumps({"termination_url": TERM_URL, "worksheet_name": TERM_WS}).encode()
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Password", "wrong-password-test")
        resp = urllib.request.urlopen(req, timeout=15)
        elapsed = time.time() - t0
        data = json.loads(resp.read())
        safe_print(f"  HTTP {resp.status} ({elapsed:.2f}s) - unexpected success")
        log_entry("K", "preview_post_401", "unexpected_success", {"status": resp.status, "data": data})
        return data.get("_v")
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        safe_print(f"  HTTP {e.code} ({elapsed:.2f}s)")
        try:
            data = json.loads(body_text)
            v = data.get("_v", "NOT FOUND")
            safe_print(f"  _v: {v}")
            safe_print(f"  error: {data.get('error', '?')}")
            log_entry("K", "preview_post_401", "auth_fail_response", {"code": e.code, "data": data, "elapsed": round(elapsed, 2)})
            return v
        except Exception:
            safe_print(f"  body: {body_text[:100]}")
            log_entry("K", "preview_post_401", "unparseable", {"code": e.code, "body": body_text, "elapsed": round(elapsed, 2)})
            return None
    except Exception as e:
        elapsed = time.time() - t0
        safe_print(f"  ERROR: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("K", "preview_post_401", "exception", {"type": type(e).__name__, "msg": str(e), "elapsed": round(elapsed, 2)})
        return None


def test_diagnose_full():
    """GET /api/diagnose?mode=full → 전체 파이프라인 성능"""
    safe_print("\n[3/3] GET /api/diagnose?mode=full (전체 파이프라인)")
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
                safe_print(f"    {name}: +{delta:.3f}s (cum {se}s)")
                prev = se
        safe_print(f"  total: {data.get('total_s', '?')}s")
        log_entry("K", "diagnose_full", "result", data)
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body = e.read().decode("utf-8", errors="replace")[:200]
        safe_print(f"  HTTP ERROR {e.code} ({elapsed:.2f}s): {body[:80]}")
        log_entry("K", "diagnose_full", "error", {"code": e.code, "elapsed": round(elapsed, 2)})
    except Exception as e:
        elapsed = time.time() - t0
        safe_print(f"  ERROR: {type(e).__name__}: {e} ({elapsed:.2f}s)")
        log_entry("K", "diagnose_full", "exception", {"type": type(e).__name__, "msg": str(e), "elapsed": round(elapsed, 2)})


def main():
    safe_print("=" * 60)
    safe_print("v4 Preview Deployment Verification")
    safe_print("=" * 60)

    # Step 1: 배포된 버전 확인
    version = test_preview_version()
    if version == "v4-fast":
        safe_print("  >>> v4-fast 배포 확인됨!")
    else:
        safe_print(f"  >>> 경고: 배포 버전이 '{version}'입니다 (v4-fast 기대)")

    # Step 2: POST 401 테스트
    v_from_401 = test_preview_auth_fail()

    # Step 3: diagnose full
    test_diagnose_full()

    safe_print(f"\n{'=' * 60}")
    safe_print("결론:")
    if version == "v4-fast" and v_from_401 == "v4-fast":
        safe_print("  v4 코드가 정상 배포됨. 프론트엔드에서 재테스트 필요.")
    elif version is None or v_from_401 is None:
        safe_print("  배포 확인 불가. GET 핸들러가 없으면 이전 버전이 배포된 것.")
    else:
        safe_print(f"  GET version={version}, POST 401 _v={v_from_401}")
    safe_print("=" * 60)


if __name__ == "__main__":
    main()
