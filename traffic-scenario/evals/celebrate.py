# 화면의 축하 연출 미리 보기. 켜져 있는 화면에 축하 단계를 보내거나 서버 없이 연출만 띄운다
import argparse
import os
import tempfile
import time
from pathlib import Path

from . import repair


# 서버 없이 축하 연출만 계속 보여 주는 화면 실행
def preview(seconds=600):
    from frontend.app import TrafficWorld, run_terminal
    from frontend.session import Session

    session = Session("http://127.0.0.1:9")
    world = TrafficWorld(session)
    session.celebrate_until = time.monotonic() + seconds
    session.agent = "PR 등록 완료: 축하 연출 미리 보기 (q로 종료)"
    run_terminal(world, Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="evals.celebrate")
    p.add_argument("--preview", action="store_true")

    if p.parse_args().preview:
        preview()
    else:
        data = Path(os.getenv("MBM_DATA_DIR", repair.ROOT / "data")).resolve()
        control = os.getenv("MBM_CONTROL_URL", "http://127.0.0.1:8000")
        repair.notifier(control, repair.latest_run(data)[0])("celebrating", "축하 연출 미리 보기입니다.")
