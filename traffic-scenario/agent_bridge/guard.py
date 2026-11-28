# 에이전트 수정 전 상태 저장과 되돌리기 및 수정 범위 확인
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"backend/catalog.py", "backend/target.py"}
SKIP = shutil.ignore_patterns("__pycache__")


# 작업 폴더의 파일별 내용 해시 조회
def hashes():
    out = subprocess.run(["git", "ls-files", "-c", "-o", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout

    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sorted(set(out.splitlines())) if (ROOT / p).is_file()}


# DB에 있는 테이블과 인덱스 이름 조회
def objects(catalog):
    con = sqlite3.connect(catalog, timeout=10)

    try:
        return sorted(f"{t}:{n}" for t, n in con.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    finally:
        con.close()


# 현재 코드와 DB 구조를 되돌리기 기준으로 저장
def snapshot(data_dir):
    home = Path(data_dir) / "agent-snapshots"
    fresh = home / uuid.uuid4().hex
    value = {"hashes": hashes(), "objects": objects(Path(data_dir) / "catalog.sqlite")}
    shutil.copytree(ROOT / "backend", fresh / "backend", ignore=SKIP)
    intact(fresh / "backend", value["hashes"])
    (fresh / "snapshot.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
    # 새 기준을 폴더째 완성한 뒤 가리키는 파일만 바꿔서 중간에 실패해도 이전 기준이 남음
    pointer = Path(data_dir) / "agent-snapshot-current"
    pointer.with_suffix(".new").write_text(fresh.name)
    os.replace(pointer.with_suffix(".new"), pointer)

    for old in home.iterdir():
        if old.name != fresh.name:
            shutil.rmtree(old, ignore_errors=True)

    (Path(data_dir) / "bridge-runs.jsonl").write_text("")

    return {"saved": True, "files": len(value["hashes"]), "objects": value["objects"]}


# backend 복사본이 기록한 해시와 같은지 확인
def intact(copy, recorded):
    for path, digest in recorded.items():
        if path.startswith("backend/"):
            file = copy / path.removeprefix("backend/")

            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise SystemExit("저장한 기준이 손상됐습니다. 아무것도 바꾸지 않았습니다: " + path)


# 지금 쓰는 기준 폴더 조회
def current(data_dir):
    pointer = Path(data_dir) / "agent-snapshot-current"
    folder = Path(data_dir) / "agent-snapshots" / pointer.read_text() if pointer.exists() else None

    if not folder or not (folder / "snapshot.json").exists():
        raise SystemExit("snapshot을 먼저 실행하세요.")

    return folder


# 저장한 기준 읽기
def load(data_dir):
    return json.loads((current(data_dir) / "snapshot.json").read_text())


# 저장한 기준 이후에 내용이 바뀐 파일이 허용 범위 안인지 확인
def scope(data_dir):
    base, now = load(data_dir)["hashes"], hashes()
    outside = sorted(p for p in base.keys() | now.keys() if base.get(p) != now.get(p) and p not in ALLOWED)

    return {"ok": not outside, "outside_allowed": outside, "allowed": sorted(ALLOWED)}


# backend 폴더와 DB 구조를 저장한 기준으로 복구
def rollback(data_dir):
    base = load(data_dir)
    copy, backend = current(data_dir) / "backend", ROOT / "backend"
    removed = []
    intact(copy, base["hashes"])

    # 기준 이후 backend에 생긴 파일은 누가 만들었는지와 무관하게 삭제됨
    for path in sorted(backend.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and not (copy / path.relative_to(backend)).exists():
            path.unlink()
            removed.append(str(path.relative_to(ROOT)))

    shutil.copytree(copy, backend, dirs_exist_ok=True)
    catalog = Path(data_dir) / "catalog.sqlite"
    con = sqlite3.connect(catalog, timeout=10)
    dropped = []

    try:
        # 트리거와 뷰와 인덱스를 먼저 지워야 테이블 삭제가 막히지 않음
        for kind in ("trigger", "view", "index", "table"):
            for item in objects(catalog):
                if item.startswith(kind + ":") and item not in base["objects"]:
                    con.execute(f'DROP {kind.upper()} IF EXISTS "{item.split(":", 1)[1]}"')
                    dropped.append(item)

        con.commit()
    finally:
        con.close()

    # 기준 이후 지워진 DB 객체와 바뀐 데이터는 복구하지 못하므로 알리기만 함
    missing = sorted(set(base["objects"]) - set(objects(catalog)))

    return {"restored": not missing, "removed_files": removed, "dropped_objects": dropped, "missing_objects": missing}
