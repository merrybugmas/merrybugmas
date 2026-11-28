# 화면의 축하 연출만 미리 보도록 가장 최근 실험에 축하 단계 전달
import os
from pathlib import Path

from . import repair

if __name__ == "__main__":
    data = Path(os.getenv("MBM_DATA_DIR", repair.ROOT / "data")).resolve()
    control = os.getenv("MBM_CONTROL_URL", "http://127.0.0.1:8000")
    repair.notifier(control, repair.latest_run(data)[0])("celebrating", "축하 연출 미리 보기입니다.")
