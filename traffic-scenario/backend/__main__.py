import argparse
import json
import os
import socket

import uvicorn

from .config import Settings
from . import catalog


# 백엔드 실행 명령 처리
def main():
    p = argparse.ArgumentParser(description="Merry Bug Mas local backend")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("seed")
    index = sub.add_parser("index")
    index.add_argument("action", choices=["add", "drop", "plan"])
    sub.add_parser("openapi")
    args = p.parse_args()
    s = Settings()
    catalog.initialize(s.catalog, s.product_count)

    if args.command == "serve":
        os.environ["MBM_CONTROL_URL"] = f"http://127.0.0.1:{args.port}"
        uvicorn.run("backend.control:create_app", factory=True, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    elif args.command == "seed":
        print(json.dumps({"catalog": str(s.catalog), "dataset_hash": catalog.dataset_hash(s.catalog)}, ensure_ascii=False))
    elif args.command == "index":
        # 실행 중 스키마 변경으로 기록된 실험 조건이 달라지지 않도록 제한
        port = s.target_port

        with socket.socket() as sock:
            running = sock.connect_ex(("127.0.0.1", port)) == 0

        if args.action != "plan" and running:
            p.error("Stop the backend before changing the index with this maintenance command")

        if args.action != "plan":
            with catalog.connect(s.catalog) as c:
                c.execute(catalog.INDEX_SQL if args.action == "add" else "DROP INDEX IF EXISTS idx_products_category_price_id")

        print(json.dumps(catalog.plan(s.catalog), ensure_ascii=False, indent=2))
    elif args.command == "openapi":
        from .control import create_app
        from .target import create_app as target_app

        folder = s.data_dir / "openapi"
        folder.mkdir(exist_ok=True)

        for name, app in [("control", create_app(s)), ("target", target_app(s))]:
            (folder / f"{name}.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2))

        print(folder)


if __name__ == "__main__":
    main()
