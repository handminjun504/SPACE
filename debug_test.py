"""
Vercel 504 디버깅 스크립트
/api/health와 /api/diagnose를 호출하여 결과를 debug.log에 기록합니다.
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE_URL = "https://space-ten-beta.vercel.app"
LOG_PATH = os.path.join(os.path.dirname(__file__), ".cursor", "debug.log")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def log_entry(hypothesis_id, location, message, data=None):
    entry = {
        "id": f"log_{int(time.time()*1000)}",
        "timestamp": int(time.time() * 1000),
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "runId": "diagnose-run-1",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"  [{hypothesis_id}] {message}: {json.dumps(data, ensure_ascii=False) if data else ''}")


def call_api(endpoint, timeout=65):
    url = f"{BASE_URL}{endpoint}"
    print(f"\n>>> 호출: {url} (timeout={timeout}s)")
    t0 = time.time()
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            elapsed = round(time.time() - t0, 2)
            print(f"<<< 응답: HTTP {status} ({elapsed}s)")
            try:
                return {"status": status, "data": json.loads(body), "elapsed": elapsed}
            except json.JSONDecodeError:
                return {"status": status, "data": body[:500], "elapsed": elapsed}
    except urllib.error.HTTPError as e:
        elapsed = round(time.time() - t0, 2)
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"<<< HTTP 에러: {e.code} ({elapsed}s) - {body[:100]}")
        return {"status": e.code, "data": body, "elapsed": elapsed}
    except urllib.error.URLError as e:
        elapsed = round(time.time() - t0, 2)
        print(f"<<< 연결 에러: {e.reason} ({elapsed}s)")
        return {"status": 0, "data": str(e.reason), "elapsed": elapsed}
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        print(f"<<< 에러: {type(e).__name__}: {e} ({elapsed}s)")
        return {"status": 0, "data": str(e), "elapsed": elapsed}


def main():
    print("=" * 60)
    print("Vercel 504 디버깅 시작")
    print("=" * 60)

    # 1) Health check (가설 D, E)
    print("\n[1/2] Health 체크...")
    health = call_api("/api/health", timeout=30)
    log_entry("D", "debug_test.py:health", "health_response", health)
    log_entry("E", "debug_test.py:health", "maxDuration_check",
              {"http_status": health["status"], "elapsed": health.get("elapsed")})

    # 2) Diagnose - 단계별 타이밍 (가설 A, B, C)
    print("\n[2/2] 진단 (최대 65초 대기)...")
    diag = call_api("/api/diagnose", timeout=65)
    log_entry("A", "debug_test.py:diagnose", "cold_start_timing", diag)

    if isinstance(diag.get("data"), dict) and "steps" in diag["data"]:
        steps = diag["data"]["steps"]
        for step in steps:
            step_name = step.get("step", "unknown")
            if "import" in step_name:
                log_entry("A", f"diagnose:{step_name}", "import_timing", step)
            elif "client" in step_name:
                log_entry("A", f"diagnose:{step_name}", "client_timing", step)
            elif "settlement_open" in step_name:
                log_entry("B", f"diagnose:{step_name}", "settlement_open_timing", step)
            elif "settlement_read" in step_name:
                log_entry("B", f"diagnose:{step_name}", "settlement_read_timing", step)
            elif "TIMEOUT" in step_name:
                log_entry("E", f"diagnose:{step_name}", "timeout_risk", step)
    else:
        log_entry("E", "debug_test.py:diagnose", "diagnose_failed",
                  {"status": diag["status"], "hint": "504면 maxDuration 미적용 또는 함수 크래시"})

    print(f"\n{'=' * 60}")
    print(f"완료! 로그 파일: {LOG_PATH}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

