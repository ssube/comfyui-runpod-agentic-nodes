from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from comfy_cpu_smoke import comfy_cmd, default_port, ensure_comfyui, wait_for_server

PROJECT_NAME = "crag-local-agent-skills-e2e"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a comprehensive ComfyUI local runtime preflight with skills, Postgres, LLM API env, startup commands, and agent prompt.")
    parser.add_argument("--comfy-dir", default=os.environ.get("COMFYUI_E2E_DIR", "/tmp/comfyui-runpod-e2e"))
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMFYUI_LOCAL_AGENT_E2E_PORT", "0")))
    parser.add_argument("--skip-clone", action="store_true", default=os.environ.get("COMFYUI_E2E_SKIP_CLONE") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the local agent skills e2e.")
    if not shutil.which("sudo"):
        raise SystemExit("sudo is required because this host's containerd runtime needs elevated nerdctl.")

    comfy_dir = Path(args.comfy_dir)
    repo_dir = Path(args.repo_dir).resolve()
    ensure_comfyui(comfy_dir, skip_clone=args.skip_clone)

    with tempfile.TemporaryDirectory(prefix="runpod-comfy-local-agent-") as tmp:
        base_dir = Path(tmp) / "base"
        user_dir = Path(tmp) / "user"
        custom_nodes = base_dir / "custom_nodes"
        custom_nodes.mkdir(parents=True)
        user_dir.mkdir(parents=True)
        (custom_nodes / "comfyui-runpod-agentic").symlink_to(repo_dir, target_is_directory=True)

        port = args.port or default_port()
        proc = subprocess.Popen(comfy_cmd(args.python, comfy_dir, base_dir, user_dir, port=port), cwd=comfy_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output: list[str] = []
        try:
            wait_for_server(port, proc, output)
            server = f"http://127.0.0.1:{port}"
            try:
                submit_workflow(server, repo_dir / "examples/workflows/api_local_agent_skills_postgres_up.json", timeout=1200)
                services = wait_for_services(PROJECT_NAME, {"agent", "sql"}, timeout=1200)
                agent = next(service for service in services if service["role"] == "agent")
                database_url = env_value(agent["id"], "DATABASE_URL")
                ollama_host = env_value(agent["id"], "OLLAMA_HOST")
                if "postgresql://" not in database_url or ":5432" not in database_url:
                    raise AssertionError(f"Agent DATABASE_URL was not wired to Postgres: {database_url}")
                if ollama_host != "https://ollama.com":
                    raise AssertionError(f"Agent OLLAMA_HOST was not wired to Ollama Cloud: {ollama_host}")
                db_host = urlparse(database_url).hostname or ""
                dns = run_runtime(["nerdctl", "exec", agent["id"], "getent", "hosts", db_host]).stdout.strip()
                if db_host not in dns:
                    raise AssertionError(f"Agent container could not resolve Postgres service {db_host}: {dns}")

                report = wait_for_file(agent["id"], "/workspace/e2e/agent-skill-report.txt", timeout=1200)
                package_marker = file_text(agent["id"], "/workspace/e2e/package-installed.txt").strip()
                jq_version = file_text(agent["id"], "/workspace/e2e/jq-version.txt").strip()
                skills = run_runtime(["nerdctl", "exec", agent["id"], "find", "/workspace/.codex/skills", "-maxdepth", "3", "-type", "f"]).stdout.strip().splitlines()
                if package_marker != "startup package installed" or not jq_version.startswith("jq-"):
                    raise AssertionError(f"Package startup command did not complete: marker={package_marker!r} jq={jq_version!r}")
                if not skills:
                    raise AssertionError("Skill framework install did not create any files under /workspace/.codex/skills.")
                if "List the skills available to you" not in report:
                    raise AssertionError(f"Agent harness did not receive the task prompt:\n{report}")
                if "skills:" not in report or "database:" not in report or "llm:" not in report:
                    raise AssertionError(f"Agent harness report is incomplete:\n{report}")
                print(
                    json.dumps(
                        {
                            "services": services,
                            "database_url": database_url,
                            "ollama_host": ollama_host,
                            "postgres_dns": dns,
                            "jq_version": jq_version,
                            "skill_file_count": len(skills),
                            "report_excerpt": report[:1200],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            finally:
                submit_workflow(server, repo_dir / "examples/workflows/api_local_agent_skills_postgres_down.json", timeout=600)
                remaining = inspect_project(PROJECT_NAME)
                if remaining:
                    cleanup_project_containers(remaining)
                    raise AssertionError(f"Local runtime cleanup left containers running: {remaining}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
    return 0


def submit_workflow(server: str, workflow: Path, *, timeout: int) -> dict:
    response = post_json(f"{server}/prompt", {"prompt": json.loads(workflow.read_text()), "client_id": uuid.uuid4().hex})
    prompt_id = response["prompt_id"]
    entry = wait_history(server, prompt_id, timeout)[prompt_id]
    status = entry.get("status", {})
    messages = status.get("messages") or []
    if status.get("completed") is False or any(message and message[0] == "execution_error" for message in messages):
        raise AssertionError(f"Workflow {workflow} failed:\n{json.dumps(entry, indent=2, sort_keys=True)}")
    return entry


def wait_history(server: str, prompt_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            history = fetch_json(f"{server}/history/{prompt_id}")
        except urllib.error.URLError:
            history = {}
        if history:
            return history
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for prompt history: {prompt_id}")


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode())


def wait_for_services(project_name: str, roles: set[str], *, timeout: int) -> list[dict[str, str]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        services = inspect_project(project_name)
        if {service["role"] for service in services} == roles:
            return services
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for local runtime services {roles}; saw {inspect_project(project_name)}")


def inspect_project(project_name: str) -> list[dict[str, str]]:
    completed = run_runtime(["nerdctl", "ps", "--format", "json"])
    services = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        name = str(data.get("Names") or data.get("Name") or "")
        if not name.startswith(f"{project_name}-"):
            continue
        container_id = data["ID"]
        inspect = json.loads(run_runtime(["nerdctl", "inspect", container_id]).stdout)[0]
        labels = inspect.get("Config", {}).get("Labels", {})
        networks = inspect.get("NetworkSettings", {}).get("Networks", {})
        services.append({"id": container_id, "name": name, "role": labels.get("comfyui-runpod-agentic.role", ""), "network": next(iter(networks), "")})
    return services


def env_value(container_id: str, key: str) -> str:
    inspect = json.loads(run_runtime(["nerdctl", "inspect", container_id]).stdout)[0]
    for entry in inspect.get("Config", {}).get("Env", []):
        if entry.startswith(f"{key}="):
            return entry.split("=", 1)[1]
    return ""


def wait_for_file(container_id: str, path: str, *, timeout: int) -> str:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        completed = subprocess.run(["sudo", "nerdctl", "exec", container_id, "cat", path], capture_output=True, text=True, check=False)
        if completed.returncode == 0:
            return completed.stdout
        last_error = completed.stderr
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for {path} in {container_id}: {last_error}")


def file_text(container_id: str, path: str) -> str:
    return run_runtime(["nerdctl", "exec", container_id, "cat", path]).stdout


def cleanup_project_containers(services: list[dict[str, str]]) -> None:
    if services:
        run_runtime(["nerdctl", "rm", "-f", *[service["id"] for service in services]])


def run_runtime(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(["sudo", *command], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: sudo {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
