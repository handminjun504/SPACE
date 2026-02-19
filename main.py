"""
스페이스액스키 계약 종료 - 정산 시트 자동 동기화 도구

사용법:
    python main.py --dry-run    # 미리보기 (실제 변경 없음)
    python main.py              # 실제 동기화 실행
    python main.py --help       # 도움말
"""

import argparse
import sys
import os
from datetime import datetime

# 프로젝트 루트를 path에 추가
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from config.settings import (
    SETTLEMENT_SHEET_ID,
    TERMINATION_SHEET_ID,
    GOOGLE_CREDENTIALS_PATH,
    FUZZY_THRESHOLD,
)
from src.sheet_reader import SheetReader
from src.matcher import ContractMatcher
from src.updater import SheetUpdater
from src.reporter import Reporter


def print_banner():
    """프로그램 시작 배너 출력"""
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   스페이스액스키 계약 종료 → 정산 시트 자동 동기화 도구    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


def validate_config():
    """설정값 유효성 검증"""
    errors = []

    if not SETTLEMENT_SHEET_ID or SETTLEMENT_SHEET_ID == "여기에_정산_시트_ID_입력":
        errors.append(
            ".env 파일의 SETTLEMENT_SHEET_ID를 설정하세요.\n"
            "  구글 시트 URL에서 /d/ 뒤의 값입니다.\n"
            "  예: https://docs.google.com/spreadsheets/d/XXXXXXXXX/edit"
        )

    if not TERMINATION_SHEET_ID or TERMINATION_SHEET_ID == "여기에_종료_시트_ID_입력":
        errors.append(
            ".env 파일의 TERMINATION_SHEET_ID를 설정하세요.\n"
            "  구글 시트 URL에서 /d/ 뒤의 값입니다."
        )

    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        errors.append(
            f"Google 서비스 계정 키 파일을 찾을 수 없습니다: {GOOGLE_CREDENTIALS_PATH}\n"
            "  1. Google Cloud Console → API 및 서비스 → 사용자 인증 정보\n"
            "  2. 서비스 계정 생성 → JSON 키 다운로드\n"
            "  3. credentials/ 폴더에 service_account.json으로 저장\n"
            "  4. 두 구글 시트에 서비스 계정 이메일로 편집 권한 공유"
        )

    if errors:
        print("[오류] 설정을 확인해주세요:\n")
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}\n")
        return False

    return True


def main():
    """메인 실행 함수"""
    parser = argparse.ArgumentParser(
        description="스페이스액스키 계약 종료 → 정산 시트 자동 동기화 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  python main.py --dry-run          미리보기 (안전 모드, 변경 없음)
  python main.py                    실제 동기화 실행
  python main.py --threshold 90     유사도 임계값 90%로 변경
  python main.py --no-report        CSV 리포트 생성 안 함
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="미리보기 모드 (실제 시트 변경 없음)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=FUZZY_THRESHOLD,
        help=f"퍼지 매칭 유사도 임계값 (기본: {FUZZY_THRESHOLD}%%)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        default=False,
        help="CSV 리포트 파일 생성 안 함",
    )

    args = parser.parse_args()

    print_banner()

    # 1. 설정 검증
    print("[1/5] 설정 검증 중...")
    if not validate_config():
        sys.exit(1)
    print("[OK] 설정 검증 완료\n")

    # 2. Google Sheets 연결 및 데이터 읽기
    print("[2/5] Google Sheets 데이터 읽는 중...")
    try:
        reader = SheetReader()
        settlement_df = reader.read_settlement_sheet()
        termination_df = reader.read_termination_sheet()
    except Exception as e:
        print(f"\n[오류] 시트 읽기 실패: {e}")
        sys.exit(1)

    print(f"  정산 시트: {len(settlement_df)}행, {len(settlement_df.columns)}열")
    print(f"  종료 시트: {len(termination_df)}행, {len(termination_df.columns)}열")
    print()

    # 3. 매칭 실행
    print("[3/5] 매칭 실행 중...")
    matcher = ContractMatcher(
        settlement_df=settlement_df,
        termination_df=termination_df,
        fuzzy_threshold=args.threshold,
    )
    match_results = matcher.run()

    # 4. 정산 시트 업데이트
    mode_text = "미리보기" if args.dry_run else "실제 업데이트"
    print(f"[4/5] 정산 시트 {mode_text} 중...")
    updater = SheetUpdater(sheet_reader=reader, dry_run=args.dry_run)
    updates_log = updater.apply_updates(match_results)

    # 5. 리포트 생성
    print("[5/5] 리포트 생성 중...")
    reporter = Reporter(match_results=match_results, updates_log=updates_log)
    reporter.print_detailed_report()

    if not args.no_report:
        report_path = reporter.save_csv_report()
        if updates_log:
            log_path = reporter.save_updates_log()

    print("\n[완료] 동기화 프로세스가 종료되었습니다.")
    if args.dry_run:
        print("[TIP] 실제 변경을 원하시면 --dry-run 없이 다시 실행하세요:")
        print("      python main.py")


if __name__ == "__main__":
    main()

