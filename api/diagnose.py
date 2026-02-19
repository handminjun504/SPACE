"""
GET /api/diagnose - 단계별 격리 진단
쿼리 파라미터:
  ?mode=settlement  → 정산 시트만 테스트 (기본)
  ?mode=termination&url=...  → 종료 시트만 테스트
  ?mode=full&url=...  → 전체 플로우 테스트
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        t0 = time.time()
        steps = []

        def elapsed():
            return round(time.time() - t0, 3)

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        mode = qs.get("mode", ["settlement"])[0]
        term_url = qs.get("url", [""])[0]
        term_ws = qs.get("ws", ["계약 종료"])[0]

        # Step 1: Import
        try:
            from _lib.sheets import get_gspread_client, extract_sheet_id, open_and_read
            from _lib.matcher import run_matching, results_to_json, summary, extract_name
            from _lib.matcher import detect_changes, changes_to_json, changes_summary
            steps.append({"step": "1_imports", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            import traceback
            steps.append({"step": "1_imports", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
            return self._respond(steps, t0, mode)

        # Step 2: Client
        try:
            client = get_gspread_client()
            steps.append({"step": "2_client", "ok": True, "elapsed_s": elapsed()})
        except Exception as e:
            steps.append({"step": "2_client", "ok": False, "error": str(e), "elapsed_s": elapsed()})
            return self._respond(steps, t0, mode)

        s_data = None
        t_data = None

        # mode=settlement (기본): 정산 시트만
        if mode in ("settlement", "full", "name_debug"):
            sid = os.environ.get("SETTLEMENT_SHEET_ID", "")
            sws = os.environ.get("SETTLEMENT_WORKSHEET_NAME", "기초데이터")
            try:
                s_title, _, s_data = open_and_read(client, sid, sws)
                s_headers = list(s_data[0].keys()) if s_data else []
                steps.append({"step": "3_settlement", "ok": True, "title": s_title, "rows": len(s_data), "headers": s_headers, "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "3_settlement", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
                return self._respond(steps, t0, mode)

        # mode=termination: 종료 시트만
        if mode in ("termination", "full", "name_debug") and term_url:
            try:
                term_id = extract_sheet_id(term_url)
                t_title, t_ws_list, t_data = open_and_read(client, term_id, term_ws)
                t_headers = list(t_data[0].keys()) if t_data else []
                t_sample = {}
                if t_data:
                    for k, v in list(t_data[0].items()):
                        t_sample[k] = str(v)[:60] if v else ""
                steps.append({"step": "4_termination", "ok": True, "title": t_title, "ws_list": t_ws_list, "rows": len(t_data), "headers": t_headers, "sample_row": t_sample, "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "4_termination", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
                return self._respond(steps, t0, mode)

        # mode=columns: 컬럼 구조 + 샘플 데이터 확인
        if mode == "columns":
            info = {}
            if s_data:
                info["settlement_columns"] = list(s_data[0].keys()) if s_data else []
                info["settlement_sample"] = s_data[:2] if len(s_data) >= 2 else s_data[:1]
            if 't_data' in dir() and t_data:
                info["termination_columns"] = list(t_data[0].keys()) if t_data else []
                info["termination_sample"] = t_data[:2] if len(t_data) >= 2 else t_data[:1]
            steps.append({"step": "columns_info", "ok": True, "data": info, "elapsed_s": elapsed()})

        # mode=full: 매칭 + 변경감지 + 데이터 품질 분석
        if mode == "full" and term_url:
            # 데이터 품질 분석
            from _lib.matcher import (
                normalize_text, normalize_name, normalize_date_str, normalize_room, get_col,
                _get_termination_dates, _prepare_termination,
                extract_name,
            )

            # ===== 종료 시트 행 상태 분석 =====
            t_empty = 0          # 완전히 빈 행
            t_no_branch = 0      # 지점명 없음
            t_has_branch = 0     # 지점명 있음
            t_branch_in_s = 0    # 정산에도 있는 지점
            t_has_name = 0       # 이름 있음
            t_matchable = 0      # 지점+이름 둘 다 있고 지점이 정산에 존재

            s_branches = set(normalize_text(get_col(r, "지점명")) for r in s_data if get_col(r, "지점명").strip())
            t_branches = set(normalize_text(get_col(r, "지점명")) for r in t_data if get_col(r, "지점명").strip())
            common_branches = s_branches & t_branches

            # 지점별 종료 행 수 세기
            from collections import Counter
            t_branch_counts = Counter()
            t_branch_in_s_counts = Counter()

            for r in t_data:
                branch_raw = get_col(r, "지점명").strip()
                name_raw = extract_name(r, "계약자명", ["주민 번호", "상호"]).strip()
                branch_norm = normalize_text(branch_raw)

                # 행이 완전히 비어있는지
                all_empty = all(not str(v).strip() for v in r.values())
                if all_empty:
                    t_empty += 1
                    continue

                if not branch_raw:
                    t_no_branch += 1
                    continue

                t_has_branch += 1
                t_branch_counts[branch_norm] += 1

                if branch_norm in common_branches:
                    t_branch_in_s += 1
                    t_branch_in_s_counts[branch_norm] += 1

                if name_raw:
                    t_has_name += 1
                    if branch_norm in common_branches:
                        t_matchable += 1

            # 정산 시트 지점별 행 수
            s_branch_counts = Counter()
            for r in s_data:
                bn = normalize_text(get_col(r, "지점명"))
                if bn:
                    s_branch_counts[bn] += 1

            # 공통 지점별 종료/정산 행 수 비교
            branch_comparison = []
            for b in sorted(common_branches):
                branch_comparison.append({
                    "branch": b[:30],
                    "t_rows": t_branch_in_s_counts.get(b, 0),
                    "s_rows": s_branch_counts.get(b, 0),
                })

            # 매칭 불가 지점 (종료에만 있는)
            t_only_branches = []
            for b in sorted(t_branches - common_branches):
                t_only_branches.append({
                    "branch": b[:40],
                    "t_rows": t_branch_counts.get(b, 0),
                })

            # 날짜 관련 (간략화)
            t_raw_start = sum(1 for r in t_data if normalize_date_str(get_col(r, "계약 시작 날짜")))
            t_raw_end = sum(1 for r in t_data if normalize_date_str(get_col(r, "계약 만기 날짜")))
            _, t_stats = _prepare_termination(t_data)

            # 매칭 가능한 행에서 이름 샘플 (처음 5개)
            matchable_samples = []
            for r in t_data:
                branch_norm = normalize_text(get_col(r, "지점명"))
                name = extract_name(r, "계약자명", ["주민 번호", "상호"]).strip()
                if branch_norm in common_branches and name and len(matchable_samples) < 5:
                    matchable_samples.append({
                        "branch": get_col(r, "지점명")[:20],
                        "name": name[:15],
                        "room": get_col(r, "호실")[:10],
                    })

            steps.append({
                "step": "4b_data_quality", "ok": True,
                # 행 상태 분석
                "t_total": len(t_data),
                "t_empty_rows": t_empty,
                "t_no_branch": t_no_branch,
                "t_has_branch": t_has_branch,
                "t_branch_in_settlement": t_branch_in_s,
                "t_has_name": t_has_name,
                "t_matchable": t_matchable,
                # 지점 분석
                "s_branch_count": len(s_branches),
                "t_branch_count": len(t_branches),
                "common_branch_count": len(common_branches),
                "branch_comparison": branch_comparison,
                "t_only_branches": t_only_branches,
                # 날짜
                "t_date_shift_corrected": t_stats["date_shift_corrected"],
                # 샘플
                "matchable_samples": matchable_samples,
                "elapsed_s": elapsed(),
            })

            try:
                results = run_matching(s_data, t_data)
                matched = sum(1 for r in results if r.settlement_index is not None)
                exact = sum(1 for r in results if r.status.value == "정확매칭")
                fuzzy = sum(1 for r in results if "유사" in r.status.value)

                # 매칭 상세 분류 (v7 Phase별)
                phase_counts = {"P1_호실+이름": 0, "P2_호실": 0, "P3_이름": 0, "P4_퍼지": 0}
                for r in results:
                    if r.settlement_index is None:
                        continue
                    d = r.match_details
                    if d.startswith("P1:"):
                        phase_counts["P1_호실+이름"] += 1
                    elif d.startswith("P2:"):
                        phase_counts["P2_호실"] += 1
                    elif d.startswith("P3:"):
                        phase_counts["P3_이름"] += 1
                    elif d.startswith("P4:"):
                        phase_counts["P4_퍼지"] += 1
                    else:
                        phase_counts["P4_퍼지"] += 1  # fallback

                rejected_count = 0  # v7에서는 hard reject 없음
                rejected_samples = []

                # 매칭된 결과 샘플 (처음 5개)
                match_samples = []
                for r in results:
                    if r.settlement_index is not None and len(match_samples) < 5:
                        t_start_corr, t_end_corr = _get_termination_dates(r.termination_row)
                        match_samples.append({
                            "t_idx": r.termination_index,
                            "s_idx": r.settlement_index,
                            "confidence": r.confidence,
                            "details": r.match_details[:150],
                            "t_name": extract_name(r.termination_row, "계약자명", ["주민 번호", "상호"]),
                            "s_name": get_col(r.settlement_row, "계약자명"),
                            "t_branch": get_col(r.termination_row, "지점명")[:20],
                            "t_room": get_col(r.termination_row, "호실")[:10],
                            "s_room": get_col(r.settlement_row, "호실")[:10],
                            "t_expiry": t_end_corr,
                            "s_end_date": get_col(r.settlement_row, "계약종료일자")[:12],
                        })

                steps.append({
                    "step": "5_matching", "ok": True,
                    "total": len(results), "matched": matched,
                    "exact": exact, "fuzzy": fuzzy,
                    "rejected_by_verify": rejected_count,
                    "phases": phase_counts,
                    "match_samples": match_samples,
                    "rejected_samples": rejected_samples,
                    "elapsed_s": elapsed(),
                })
            except Exception as e:
                import traceback
                steps.append({"step": "5_matching", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})
                return self._respond(steps, t0, mode)

            try:
                changes = detect_changes(results)
                matched_results = [r for r in results if r.settlement_index is not None]
                rj = results_to_json(matched_results)
                cj = changes_to_json(changes)
                resp_size = len(json.dumps({"r": rj, "c": cj}, ensure_ascii=False))
                steps.append({"step": "6_serialize", "ok": True, "changes": len(changes), "resp_bytes": resp_size, "elapsed_s": elapsed()})
            except Exception as e:
                import traceback
                steps.append({"step": "6_serialize", "ok": False, "error": str(e), "tb": traceback.format_exc()[-300:], "elapsed_s": elapsed()})

        # === 이름 매칭 디버깅 (mode=name_debug) ===
        if mode == "name_debug" and s_data and t_data:
            from _lib.matcher import normalize_text, normalize_name, normalize_room, get_col, extract_name
            from collections import Counter

            target_branch = qs.get("branch", [""])[0]

            # 지점별 정산/종료 이름 수집
            s_names_by_branch = {}
            for r in s_data:
                bn = normalize_text(get_col(r, "지점명"))
                if not bn:
                    continue
                name = normalize_name(get_col(r, "계약자명"))
                room = normalize_room(get_col(r, "호실"))
                if bn not in s_names_by_branch:
                    s_names_by_branch[bn] = []
                s_names_by_branch[bn].append({"name": name, "room": room, "raw_name": get_col(r, "계약자명")[:20], "raw_room": get_col(r, "호실")[:10]})

            t_names_by_branch = {}
            for r in t_data:
                bn = normalize_text(get_col(r, "지점명"))
                if not bn:
                    continue
                name = normalize_name(extract_name(r, "계약자명", ["주민 번호", "상호"]))
                room = normalize_room(get_col(r, "호실"))
                if bn not in t_names_by_branch:
                    t_names_by_branch[bn] = []
                t_names_by_branch[bn].append({"name": name, "room": room, "raw_name": extract_name(r, "계약자명", ["주민 번호", "상호"])[:20], "raw_room": get_col(r, "호실")[:10]})

            # 지점별 이름 교집합 분석
            branch_analysis = []
            for bn in sorted(set(s_names_by_branch.keys()) & set(t_names_by_branch.keys())):
                s_name_set = set(e["name"] for e in s_names_by_branch[bn] if e["name"])
                t_name_set = set(e["name"] for e in t_names_by_branch[bn] if e["name"])
                overlap = s_name_set & t_name_set

                # Room overlap
                s_room_set = set(e["room"] for e in s_names_by_branch[bn] if e["room"])
                t_room_set = set(e["room"] for e in t_names_by_branch[bn] if e["room"])
                room_overlap = s_room_set & t_room_set

                entry = {
                    "branch": bn[:30],
                    "s_names": len(s_name_set),
                    "t_names": len(t_name_set),
                    "name_overlap": len(overlap),
                    "s_rooms": len(s_room_set),
                    "t_rooms": len(t_room_set),
                    "room_overlap": len(room_overlap),
                }

                # 이름 샘플: 한글 이름만 필터 (전화번호/주민번호 제외)
                import re as _re
                korean_t = sorted([n for n in t_name_set if _re.match(r'^[가-힣]{2,5}$', n)])
                korean_s = sorted([n for n in s_name_set if _re.match(r'^[가-힣]{2,5}$', n)])
                entry["t_korean_names"] = len(korean_t)
                entry["s_korean_names"] = len(korean_s)
                entry["t_korean_sample"] = korean_t[:10]
                entry["s_korean_sample"] = korean_s[:10]
                korean_overlap = set(korean_t) & set(korean_s)
                entry["korean_overlap"] = len(korean_overlap)
                if korean_overlap:
                    entry["korean_overlap_names"] = sorted(list(korean_overlap))[:10]
                
                # Raw 이름 (첫 5행)
                entry["t_raw_first5"] = [e["raw_name"] for e in t_names_by_branch[bn][:5]]
                entry["s_raw_first5"] = [e["raw_name"] for e in s_names_by_branch[bn][:5]]

                branch_analysis.append(entry)

            # 정산 시트 날짜 범위 확인
            s_dates = []
            for r in s_data[:100]:  # 첫 100행만
                d = get_col(r, "계약시작일자")
                if d.strip():
                    s_dates.append(d.strip())
            t_dates = []
            for r in t_data[:100]:
                d = get_col(r, "계약 시작 날짜")
                if d.strip():
                    t_dates.append(d.strip())
            
            # 정산 시트 계약월 확인
            s_months = set()
            for r in s_data[:200]:
                m = get_col(r, "계약월(자동)")
                if m.strip():
                    s_months.add(m.strip())

            steps.append({
                "step": "name_debug", "ok": True,
                "branch_count": len(branch_analysis),
                "s_date_range": {"first": s_dates[:3], "last": s_dates[-3:]},
                "t_date_range": {"first": t_dates[:3], "last": t_dates[-3:]},
                "s_months_sample": sorted(list(s_months))[:10],
                "branches": branch_analysis,
                "elapsed_s": elapsed(),
            })

        self._respond(steps, t0, mode)

    def _respond(self, steps, t0, mode="?"):
        result = {"mode": mode, "steps": steps, "total_s": round(time.time() - t0, 3), "py": sys.version[:10]}
        body = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
