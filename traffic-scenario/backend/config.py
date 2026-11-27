from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MBM_DATA_DIR", ROOT / "data")).resolve())
    control_url: str = field(default_factory=lambda: os.getenv("MBM_CONTROL_URL", "http://127.0.0.1:8000"))
    target_url: str = field(default_factory=lambda: os.getenv("MBM_TARGET_URL", "http://127.0.0.1:8001"))
    target_port: int = field(default_factory=lambda: int(os.getenv("MBM_TARGET_PORT", "8001")))
    product_count: int = field(default_factory=lambda: int(os.getenv("MBM_PRODUCT_COUNT", "100000")))
    concurrency: int = field(default_factory=lambda: int(os.getenv("MBM_CONCURRENCY", "4")))
    queue_limit: int = field(default_factory=lambda: int(os.getenv("MBM_QUEUE_LIMIT", "200")))
    queue_timeout_ms: int = field(default_factory=lambda: int(os.getenv("MBM_QUEUE_TIMEOUT_MS", "3000")))
    max_inflight: int = field(default_factory=lambda: int(os.getenv("MBM_MAX_INFLIGHT", "1000")))
    manage_target: bool = field(default_factory=lambda: os.getenv("MBM_MANAGE_TARGET", "1") == "1")
    policies_path: Path = field(default_factory=lambda: Path(os.getenv("MBM_POLICIES", ROOT / "config" / "policies.json")))

    # 서버 설정값 검증
    def __post_init__(self):
        if min(self.product_count, self.concurrency, self.queue_limit, self.queue_timeout_ms, self.max_inflight) < 1:
            raise ValueError("All numeric server settings must be positive")

        for url in (self.control_url, self.target_url):
            if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("This backend only supports loopback URLs")

        self.data_dir.mkdir(parents=True, exist_ok=True)

    # 상품 DB 경로 반환
    @property
    def catalog(self):
        return self.data_dir / "catalog.sqlite"

    # 실험 DB 경로 반환
    @property
    def experiments(self):
        return self.data_dir / "experiments.sqlite"

    # 실험 기록 폴더 준비
    @property
    def runs_dir(self):
        p = self.data_dir / "runs"
        p.mkdir(exist_ok=True)

        return p
