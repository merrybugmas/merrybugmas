# 폴더 생성부터 유입과 Codex 실행과 채점까지 평가 한 번 실행
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import make_workspace, score_run

PROMPT = "$performance-repair 기획전 트래픽이 유입됐다. 고객이 제시간에 올바른 검색 결과를 받는지 확인하고 변경이 필요하면 근거와 함께 수정하라."


# Codex에 줄 권한. 작업 폴더의 서버 코드와 DB만 쓰고 심판 폴더는 실험 기록만 읽음
def permissions(info):
    root, judge, work = info["root"], info["judge"], info["workspace"]
    rules = {":root": "deny", ":minimal": "read", root: "deny", work: "write", f"{judge}/data/runs": "read",
             f"{work}/.agents": "read", f"{work}/AGENTS.md": "read", f"{work}/agent_bridge": "read", f"{work}/data/runs": "read"}
    files = ", ".join(f'"{k}"="{v}"' for k, v in rules.items())

    return f'permissions.eval={{extends=":workspace", filesystem={{{files}}}, network={{enabled=true}}}}'


# 주소가 응답할 때까지 대기
def ready(url, tries=150):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(0.2)

    return False


# 심판 폴더에서 실험 도구 명령 실행
def bridge(info, env, *argv):
    done = subprocess.run([sys.executable, "-B", "-m", "agent_bridge", *argv], cwd=info["judge"], env=env, capture_output=True, text=True)

    return json.loads(done.stdout) if done.returncode == 0 else {"error": done.stderr.strip()}


# 평가 한 번 실행
def run_once(state, dest, catalog, rules="study", effort="medium", timeout=900, port=8000, seed=None, agent=True):
    info = make_workspace.build(state, dest, catalog, rules, port)
    root = Path(info["root"])
    env = {**os.environ, "MBM_PRODUCT_COUNT": str(info["product_count"]), "MBM_POLICIES": "agent_bridge/policies.json",
           "MBM_TARGET_PORT": str(port + 1), "MBM_TARGET_URL": f"http://127.0.0.1:{port + 1}", "MBM_CONTROL_URL": f"http://127.0.0.1:{port}"}
    env.pop("MBM_DATA_DIR", None)
    procs, result = [], {"state": state, "rules": rules, "effort": effort}

    try:
        with (root / "control.log").open("w") as log:
            procs.append(subprocess.Popen([sys.executable, "-B", "-m", "backend", "serve", "--port", str(port)], cwd=info["judge"], env=env, stdout=log, stderr=log))

        procs.append(subprocess.Popen([sys.executable, "-B", "-m", "evals.judge", "--port", str(port + 2)], cwd=info["judge"], env=env))

        if not ready(f"http://127.0.0.1:{port}/health") or not ready(f"http://127.0.0.1:{port + 2}/"):
            raise SystemExit("서버 또는 심판 프로세스가 준비되지 않았습니다. control.log를 확인하세요.")

        # 측정이 무효인 유입을 넘기면 에이전트가 할 일이 없으므로 유효할 때까지 세 번까지 다시 보냄
        for attempt in range(3):
            load = bridge(info, env, "run", "--random", *(["--seed", str(seed + attempt)] if seed is not None else []))

            if load.get("judgement") != "measurement_invalid":
                break

        result["load"] = {k: load.get(k) for k in ("run_id", "config", "judgement", "customer_success_rate", "error")}
        result["load"]["attempts"] = attempt + 1

        if load.get("judgement") in (None, "measurement_invalid"):
            raise SystemExit(json.dumps({"error": "LOAD_INVALID", "load": result["load"]}, ensure_ascii=False))


        if agent:
            command = ["codex", "exec", "-C", info["workspace"], "-c", 'default_permissions="eval"', "-c", permissions(info),
                       "-c", f'model_reasoning_effort="{effort}"', "--json", "-o", str(root / "codex-last.txt"), PROMPT]
            started = time.monotonic()

            with (root / "codex.jsonl").open("w") as out, (root / "codex.err").open("w") as err:
                try:
                    code = subprocess.run(command, stdout=out, stderr=err, stdin=subprocess.DEVNULL, timeout=timeout).returncode
                    result["timed_out"] = False
                except subprocess.TimeoutExpired:
                    code, result["timed_out"] = None, True

            result.update(exit_code=code, elapsed_s=round(time.monotonic() - started, 1))
            result.update(score_run.score(root / "codex.jsonl", info["workspace"], state))
            # 심판 폴더의 추적 파일이 바뀌었으면 격리가 깨진 것. 두 서버 파일은 링크라서 여기에 잡히지 않음
            changed = subprocess.run(["git", "status", "--porcelain"], cwd=root / "judge", capture_output=True, text=True).stdout.splitlines()
            result["judge_changed"] = sorted(line[3:] for line in changed)
            result["passed"] = result["passed"] and not result["judge_changed"] and not result["timed_out"]
    finally:
        for proc in procs:
            proc.terminate()

        for proc in procs:
            proc.wait(timeout=20)

    (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="evals.runner")
    p.add_argument("state", choices=sorted(make_workspace.STATES))
    p.add_argument("dest")
    p.add_argument("--catalog", required=True)
    p.add_argument("--rules", choices=["study", "baseline"], default="study")
    p.add_argument("--effort", default="medium")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--seed", type=int)
    p.add_argument("--skip-agent", action="store_true")
    args = p.parse_args()
    print(json.dumps(run_once(args.state, args.dest, args.catalog, args.rules, args.effort, args.timeout, args.port, args.seed, not args.skip_agent),
                     ensure_ascii=False, indent=2))
