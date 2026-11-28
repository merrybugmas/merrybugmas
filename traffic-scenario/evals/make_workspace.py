# 검증 쪽의 심판 폴더와 에이전트의 작업 폴더를 따로 만드는 평가 준비
import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_products_category_price_id ON products(category,price,product_id)"
# 출발 상태마다 검색 인덱스 유무와 적용할 결함 파일 지정
STATES = {"a": (False, None), "b": (True, "stock_recheck.patch"), "c": (True, None)}
# 에이전트가 고칠 수 있는 파일. 심판 폴더에서는 작업 폴더의 파일을 가리키는 링크로 둠
EDITABLE = ("catalog.py", "target.py")
# 작업 폴더의 명령 도구. 판정 로직 없이 심판 프로세스에 명령만 전달
CLIENT = '''# 실험 도구 명령을 심판 프로세스에 전달
import json
import sys
import urllib.request

request = urllib.request.Request("http://127.0.0.1:{port}/", data=json.dumps(sys.argv[1:]).encode())

try:
    with urllib.request.urlopen(request, timeout=1800) as response:
        result = json.load(response)
except OSError as exc:
    sys.exit(json.dumps({{"error": "JUDGE_UNREACHABLE", "message": str(exc)}}, ensure_ascii=False))

print(result["output"], end="")
sys.exit(result["code"])
'''


# 명령 실행 후 실패하면 중단
def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


# 파일에서 지정한 글을 바꾸고 없으면 중단
def swap(path, old, new):
    text = path.read_text()

    if old not in text:
        raise SystemExit(f"{path.name}에서 바꿀 내용을 찾지 못했습니다. 원본 코드가 바뀌었는지 확인하세요: {old[:40]}")

    path.write_text(text.replace(old, new, 1))


# 시작 글부터 끝 글 앞까지 삭제
def drop(path, start, end):
    text = path.read_text()

    if start not in text:
        raise SystemExit(f"{path.name}에서 지울 내용을 찾지 못했습니다: {start[:40]}")

    head = text.index(start)
    path.write_text(text[:head] + text[text.index(end, head):])


# 현재 상태를 첫 커밋으로 하는 저장소 생성. 결함을 넣은 흔적이 이력에 남지 않음
def commit(folder):
    run("git", "init", "-q", cwd=folder)
    (folder / ".git" / "info" / "exclude").write_text(".venv\n__pycache__/\ndata/\n")
    run("git", "add", "-A", cwd=folder)
    run("git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", "commit", "-q", "-m", "baseline", cwd=folder)


# 원본 저장소의 추적 파일을 복사하고 출발 상태의 결함 적용
def copy_source(dest, fault):
    # 커밋되지 않은 내용도 포함하도록 추적 중인 파일을 작업 폴더에서 그대로 복사
    for name in run("git", "ls-files", "-z", "traffic-scenario", cwd=REPO).split("\0"):
        if name and (REPO / name).is_file():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, dest / name)

    if fault:
        run("git", "init", "-q", cwd=dest)
        run("git", "apply", str(ROOT / "agent_bridge" / "faults" / fault), cwd=dest)
        shutil.rmtree(dest / ".git")


# 에이전트가 읽는 문서와 서버 코드에서 답이 되는 내용 제거
def strip_answers(work):
    for name in ("AGENTS.md", ".agents/skills/performance-repair/SKILL.md"):
        path = work / name
        # 작업 폴더에 없는 것을 가리키는 줄이 남으면 에이전트가 찾다가 명령이 실패함
        lines = [line for line in path.read_text().splitlines(keepends=True) if not any(k in line for k in ("pytest", "agent_bridge/README.md", "agent_bridge/faults"))]
        # 작업 폴더의 명령 도구는 표준 라이브러리만 써서 가상 환경이 필요 없음
        path.write_text("".join(lines).replace(".venv/bin/python", "python3"))

    drop(work / "backend" / "catalog.py", "INDEX_SQL = ", "\n")
    entry = work / "backend" / "__main__.py"
    drop(entry, '    index = sub.add_parser("index")', '    sub.add_parser("openapi")')
    drop(entry, '    elif args.command == "index":', '    elif args.command == "openapi":')
    readme = work / "README.md"
    swap(readme, "생성하며, 기본 상태에는 검색 인덱스가 없다.", "생성한다.")
    drop(readme, "## 인덱스 개선을 수동 확인하는 방법", "실제 조회 계획과 효과는")
    drop(work / ".agents" / "skills" / "performance-repair" / "references" / "observing.md", "- 각 조회가 표 전체를 읽는지", "- 조회 하나를 따로 실행해")


# 사전 가설 규칙을 뺀 비교군용 절차 생성
def strip_study_rule(work):
    skill = work / ".agents" / "skills" / "performance-repair" / "SKILL.md"
    skill.write_text(re.sub(r"<!-- study-rule:start -->.*?<!-- study-rule:end -->\n", "", skill.read_text(), flags=re.S))


# 상품 DB를 작업 폴더에 복사해 출발 상태에 맞춤
def prepare_data(work, judge, state, catalog):
    indexed, fault = STATES[state]
    shutil.copy(catalog, work / "data" / "catalog.sqlite")
    con = sqlite3.connect(work / "data" / "catalog.sqlite")
    con.execute(INDEX_SQL if indexed else "DROP INDEX IF EXISTS idx_products_category_price_id")
    con.execute("DROP TABLE IF EXISTS stock")
    count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    con.commit()
    con.close()

    # 결함이 재고 표를 쓰면 심판 폴더의 코드로 재고 데이터를 만듦
    if fault:
        run(sys.executable, "-B", "-c", f"from pathlib import Path; from backend import catalog; catalog.initialize(Path('data/catalog.sqlite'), {count})", cwd=judge)

    return count


# 심판 폴더와 작업 폴더 생성
def build(state, dest, catalog=None, rules="study", port=8000):
    dest = Path(dest).resolve()

    if dest.exists():
        raise SystemExit(f"이미 있는 폴더입니다: {dest}")

    fault = STATES[state][1]
    judge, work = dest / "judge" / "traffic-scenario", dest / "work" / "traffic-scenario"
    copy_source(dest / "judge", fault)

    # 검증기와 정책과 결함 파일과 테스트는 답이 되므로 작업 폴더에 넣지 않음
    for name in ("AGENTS.md", "README.md"):
        work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(judge / name, work / name)

    shutil.copytree(judge / ".agents", work / ".agents")
    shutil.copytree(judge / "backend", work / "backend")
    strip_answers(work)

    if rules == "baseline":
        strip_study_rule(work)

    (work / "agent_bridge").mkdir()
    (work / "agent_bridge" / "__main__.py").write_text(CLIENT.format(port=port + 2))

    # 심판 폴더의 서버는 에이전트가 고친 두 파일을 그대로 실행함
    for name in EDITABLE:
        (judge / "backend" / name).unlink()
        (judge / "backend" / name).symlink_to(work / "backend" / name)

    # 실험 기록과 정답은 심판 폴더에 두고 작업 폴더에서는 읽기만 하도록 링크로 연결
    (judge / "data" / "runs").mkdir(parents=True)
    (work / "data").mkdir()
    (work / "data" / "runs").symlink_to(judge / "data" / "runs")
    (judge / "data" / "catalog.sqlite").symlink_to(work / "data" / "catalog.sqlite")
    count = None

    if (ROOT / ".venv").exists():
        (judge / ".venv").symlink_to(ROOT / ".venv")

    if catalog:
        count = prepare_data(work, judge, state, Path(catalog))

    commit(dest / "judge")
    commit(dest / "work")

    return {"root": str(dest), "judge": str(judge), "workspace": str(work), "state": state, "rules": rules,
            "product_count": count, "port": port}


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="evals.make_workspace")
    p.add_argument("state", choices=sorted(STATES))
    p.add_argument("dest")
    p.add_argument("--catalog")
    p.add_argument("--rules", choices=["study", "baseline"], default="study")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    print(json.dumps(build(args.state, args.dest, args.catalog, args.rules, args.port), ensure_ascii=False, indent=2))
