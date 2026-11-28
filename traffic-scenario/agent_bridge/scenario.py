# 시연 전에 검색 서버를 정해진 출발 상태로 맞추는 준비 명령
import os
import socket
import sqlite3
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAULTS = Path(__file__).parent / "faults"
INDEX = "idx_products_category_price_id"
INDEX_SQL = f"CREATE INDEX IF NOT EXISTS {INDEX} ON products(category,price,product_id)"
LEFTOVER = "backend/에 출발 상태와 다른 변경이 남아 있습니다. rollback 하거나 직접 정리한 뒤 다시 실행하세요."
# 이름마다 상품 검색 인덱스 유무와 심어 둘 결함 파일 지정
STATES = {"a": (False, None), "b": (True, "stock_recheck.patch"), "c": (True, None)}


# 결함 파일을 코드에 적용하거나 제거
def patch(name, reverse=False):
    args = ["git", "apply", "--whitespace=nowarn"] + (["-R"] if reverse else []) + [str(FAULTS / name)]

    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


# backend 폴더가 커밋된 코드에 지정한 결함 파일만 더한 내용과 같은지 확인
def matches(fault):
    with tempfile.TemporaryDirectory() as folder:
        # 사용자의 스테이징 상태를 건드리지 않도록 임시 인덱스에서 비교
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(folder) / "index")}
        steps = [["git", "read-tree", "HEAD"]] + ([["git", "apply", "--cached", str(FAULTS / fault)]] if fault else [])
        steps.append(["git", "diff", "--quiet", "--", "backend"])

        if any(subprocess.run(step, cwd=ROOT, env=env, capture_output=True).returncode for step in steps):
            return False

        # 스테이징만 한 새 파일도 잡도록 같은 임시 인덱스 기준으로 조회
        extra = subprocess.run(["git", "ls-files", "-o", "--exclude-standard", "--", "backend"], cwd=ROOT, env=env, capture_output=True, text=True)

        return extra.returncode == 0 and not extra.stdout.strip()


# 검색 서버를 지정한 출발 상태로 변경
def prepare(name, catalog, target_port):
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", target_port)) == 0:
            raise SystemExit("서버를 끈 뒤에 실행하세요.")

    indexed, fault = STATES[name]
    known = [None] + sorted(p.name for p in FAULTS.glob("*.patch"))
    # 남은 변경 위에 결함을 덧씌우면 코드와 DB가 어긋나므로 알려진 출발 상태일 때만 진행
    current = next((f for f in known if matches(f)), False)

    if current is False:
        raise SystemExit(LEFTOVER)

    con = sqlite3.connect(catalog, timeout=10)

    try:
        # DB 준비 실패로 결함 파일만 적용된 채 남지 않도록 코드보다 먼저 확인
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone():
            raise SystemExit("상품 DB가 준비되지 않았습니다. seed를 먼저 실행하세요.")

        if current != fault:
            for item, reverse in ((current, True), (fault, False)):
                if item and patch(item, reverse).returncode:
                    raise SystemExit("결함 파일을 바꾸지 못했습니다: " + item)

        try:
            con.execute(INDEX_SQL if indexed else f"DROP INDEX IF EXISTS {INDEX}")

            # 재고 표가 남아 있으면 검증기가 재고 규칙을 기준 정답에 적용함
            if not fault:
                con.execute("DROP TABLE IF EXISTS stock")

            con.commit()
        except sqlite3.Error:
            # DB 변경이 실패하면 방금 바꾼 코드를 원래 출발 상태로 복구
            if current != fault:
                for item, reverse in ((fault, True), (current, False)):
                    if item:
                        patch(item, reverse)

            raise
    finally:
        con.close()

    return {"scenario": name, "search_index": indexed, "fault": fault}
