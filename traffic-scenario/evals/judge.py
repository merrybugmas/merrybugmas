# 작업 폴더에서 온 명령을 심판 폴더의 실험 도구로 실행하는 심판 프로세스
import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 에이전트에게 열어 주는 명령. 출발 상태 변경과 유입 전달은 받지 않음
ALLOWED = {"snapshot", "adopt", "replay", "run", "verify", "status", "logs", "scope", "restart", "compare", "rollback", "progress"}


# 명령 하나를 심판 폴더에서 실행
def execute(argv):
    if not isinstance(argv, list) or not argv or argv[0] not in ALLOWED or not all(isinstance(a, str) for a in argv):
        return {"output": json.dumps({"error": "COMMAND_NOT_ALLOWED", "allowed": sorted(ALLOWED)}, ensure_ascii=False) + "\n", "code": 2}

    done = subprocess.run([sys.executable, "-B", "-m", "agent_bridge", *argv], cwd=ROOT, capture_output=True, text=True)

    return {"output": done.stdout or done.stderr, "code": done.returncode}


# 작업 폴더의 명령 요청 수신
class Handler(BaseHTTPRequestHandler):
    # 명령 요청 처리
    def do_POST(self):
        try:
            result = execute(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        except ValueError:
            result = {"output": json.dumps({"error": "BAD_REQUEST"}) + "\n", "code": 2}

        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # 접속 기록이 에이전트 실행 기록과 섞이지 않도록 끔
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="evals.judge")
    p.add_argument("--port", type=int, default=8002)
    ThreadingHTTPServer(("127.0.0.1", p.parse_args().port), Handler).serve_forever()
