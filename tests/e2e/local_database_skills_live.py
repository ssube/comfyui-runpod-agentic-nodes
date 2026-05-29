from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from comfyui_runpod_agentic.nodes import (
    AgentNode,
    DeployNode,
    LocalSQLDatabaseNode,
    RemoteSQLDatabaseNode,
    RunLocalContainersNode,
    SSHCommandNode,
    VectorDatabaseNode,
)

CASES = ("sqlite", "postgres", "mysql", "chroma")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live local container e2e tests for CRAG database skills.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-prefix", default=f"crag-db-skills-{os.getpid()}")
    parser.add_argument("--output-dir", default="artifacts/local-runtime/database-skills")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--case", action="append", choices=CASES, help="Limit to one or more database cases.")
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the live local database skills e2e.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")
    if args.sudo_runtime:
        os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"

    old_skip = os.environ.get("CRAG_SKIP_HARNESS_INSTALL")
    os.environ["CRAG_SKIP_HARNESS_INSTALL"] = "1"
    try:
        selected = args.case or list(CASES)
        results = [run_case(args, case) for case in selected]
    finally:
        if old_skip is None:
            os.environ.pop("CRAG_SKIP_HARNESS_INSTALL", None)
        else:
            os.environ["CRAG_SKIP_HARNESS_INSTALL"] = old_skip

    print(json.dumps({"cases": results}, indent=2, sort_keys=True))
    return 0


def run_case(args: argparse.Namespace, case: str) -> dict[str, object]:
    project_name = f"{args.project_prefix}-{case}"
    output_path = str(Path(args.output_dir) / f"{case}.yaml")
    report_path = f"/workspace/e2e/{case}-database-report.json"
    deployment = build_deployment(case, report_path)
    node = RunLocalContainersNode()
    try:
        result_text, response, errors, _compose_yaml, saved_path, _image = node.apply(
            deployment,
            engine=args.engine,
            prompt=f"Verify CRAG database skill for {case}.",
            project_name=project_name,
            output_path=output_path,
            action="apply_and_wait",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
            response_path="",
            response_timeout_seconds=0,
            reuse_policy="always_create",
        )
        result = json.loads(result_text)
        if result["returncode"] != 0:
            raise AssertionError(f"{case} apply_and_wait failed:\n{result_text}\n{errors}")
        try:
            response = wait_for_agent_file(project_name, args.sudo_runtime, report_path, args.timeout_seconds)
            report = json.loads(response)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{case} did not return a database skill JSON report.\nResult:\n{result_text}\nResponse:\n{response!r}\nErrors:\n{errors}") from exc
        verify_report(case, report)
        services = inspect_project(project_name, args.sudo_runtime)
        expected_roles = {"agent"} if case in {"sqlite", "chroma"} else {"agent", "sql"}
        roles = {service["role"] for service in services}
        if roles != expected_roles:
            raise AssertionError(f"{case} expected roles {expected_roles}, got {roles}: {services}")
        return {"case": case, "compose_path": saved_path, "services": services, "report": report}
    finally:
        node.apply(
            deployment,
            prompt=f"Terminate CRAG database skill test for {case}.",
            project_name=project_name,
            output_path=output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=180,
            response_timeout_seconds=0,
            reuse_policy="always_create",
        )


def build_deployment(case: str, report_path: str):
    db = None
    vector = None
    if case == "sqlite":
        db = LocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]
    elif case == "postgres":
        db = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app")[0]
    elif case == "mysql":
        db = RemoteSQLDatabaseNode().build("MySQL", "own_pod", "app", "app")[0]
    elif case == "chroma":
        vector = VectorDatabaseNode().build("Chroma", "embedded", "docs", "/workspace/vector")[0]
    else:
        raise ValueError(f"Unsupported case: {case}")
    agent = AgentNode().build("Pi", f"{case}-database-skill", "wait_for_commands", "/workspace", sql_database=db, vector_database=vector, node_id=f"database-skill-{case}")[0]
    command = SSHCommandNode().build(probe_command(case, report_path), "before_start", "fail")[0]
    return DeployNode().build(agent, commands=command, node_id=f"database-skill-deploy-{case}")[0]


def probe_command(case: str, report_path: str) -> str:
    if case == "sqlite":
        setup = 'sqlite3 "$DATABASE_PATH" "create table if not exists crag_sqlite_items(id integer primary key, name text);"'
    elif case == "postgres":
        setup = """
for _ in $(seq 1 120); do
  if psql "$DATABASE_URL" -Atc 'select 1' >/dev/null 2>&1; then break; fi
  sleep 2
done
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -Atc 'create table if not exists crag_postgres_items(id serial primary key, name text);'
"""
    elif case == "mysql":
        setup = """
for _ in $(seq 1 120); do
  if mysql --batch --skip-column-names -h "$DATABASE_HOST" -P "$DATABASE_PORT" -u "$DATABASE_USER" -p"$DATABASE_PASSWORD" "$DATABASE_NAME" -e 'select 1' >/dev/null 2>&1; then break; fi
  sleep 2
done
mysql --batch --skip-column-names -h "$DATABASE_HOST" -P "$DATABASE_PORT" -u "$DATABASE_USER" -p"$DATABASE_PASSWORD" "$DATABASE_NAME" -e 'create table if not exists crag_mysql_items(id integer primary key auto_increment, name text);'
"""
    elif case == "chroma":
        setup = """
python3 - <<'PY'
import os
import chromadb

client = chromadb.PersistentClient(path=os.environ["VECTOR_PERSISTENCE_PATH"])
client.get_or_create_collection(os.environ["VECTOR_COLLECTION"])
PY
"""
    else:
        raise ValueError(f"Unsupported case: {case}")
    tmp_report_path = f"{report_path}.tmp"
    return f"""set -e
mkdir -p /workspace/e2e "$(dirname {report_path!r})"
{setup}
cat > /usr/local/bin/pi <<'CRAG_FAKE_PI'
#!/usr/bin/env bash
set -euo pipefail
test -f "$CRAG_RUNTIME_DIR/skills/crag-database/SKILL.md"
grep -q '^name: crag-database$' "$CRAG_RUNTIME_DIR/skills/crag-database/SKILL.md"
python3 "$CRAG_RUNTIME_DIR/skills/crag-database/list_resources.py" > {tmp_report_path!r}
mv {tmp_report_path!r} {report_path!r}
cat {report_path!r}
CRAG_FAKE_PI
chmod +x /usr/local/bin/pi
"""


def verify_report(case: str, report: dict[str, object]) -> None:
    skill_path = str(report.get("skill_path") or "")
    if not skill_path.endswith("/crag-database"):
        raise AssertionError(f"{case} skill path did not point at crag-database: {skill_path}")
    if case in {"sqlite", "postgres", "mysql"}:
        expected = f"crag_{case}_items"
        tables = (report.get("sql") or {}).get("tables") or []
        if expected not in tables:
            raise AssertionError(f"{case} expected table {expected!r}, got {tables!r}")
    if case == "chroma":
        collections = (report.get("vector") or {}).get("collections") or []
        if "docs" not in collections:
            raise AssertionError(f"chroma expected collection 'docs', got {collections!r}")


def containerd_runtime_ready(use_sudo: bool) -> bool:
    command = ["nerdctl", "info"]
    if use_sudo:
        command = ["sudo", *command]
    return subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0


def wait_for_agent_file(project_name: str, use_sudo: bool, path: str, timeout_seconds: int) -> str:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        services = inspect_project(project_name, use_sudo)
        agent_id = next((service["id"] for service in services if service["role"] == "agent"), "")
        if not agent_id:
            last_error = f"No running agent container found for {project_name}."
            time.sleep(1)
            continue
        completed = run_runtime(["nerdctl", "exec", agent_id, "cat", path], use_sudo, check=False)
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout
        logs = run_runtime(["nerdctl", "logs", agent_id], use_sudo, check=False)
        last_error = "\n".join(part for part in (completed.stderr, logs.stdout, logs.stderr) if part)
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for {path} in {project_name}.\n{last_error}")


def inspect_project(project_name: str, use_sudo: bool) -> list[dict[str, str]]:
    completed = run_runtime(["nerdctl", "ps", "--format", "json"], use_sudo)
    services = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        name = str(data.get("Names") or data.get("Name") or "")
        if not name.startswith(f"{project_name}-"):
            continue
        container_id = data["ID"]
        inspect = json.loads(run_runtime(["nerdctl", "inspect", container_id], use_sudo).stdout)[0]
        labels = inspect.get("Config", {}).get("Labels", {})
        networks = inspect.get("NetworkSettings", {}).get("Networks", {})
        services.append({"id": container_id, "name": name, "role": labels.get("comfyui-runpod-agentic.role", ""), "network": next(iter(networks), "")})
    return services


def run_runtime(command: list[str], use_sudo: bool, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if use_sudo:
        command = ["sudo", *command]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    sys.exit(main())
