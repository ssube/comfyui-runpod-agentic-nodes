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

from comfy_cpu_smoke import comfy_cmd, default_port, ensure_comfyui, wait_for_server

PROJECT_NAME = "crag-comfy-local-e2e"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a ComfyUI API e2e test that applies the CRAG local container runtime.")
    parser.add_argument("--comfy-dir", default=os.environ.get("COMFYUI_E2E_DIR", "/tmp/comfyui-runpod-e2e"))
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMFYUI_LOCAL_RUNTIME_E2E_PORT", "0")))
    parser.add_argument("--skip-clone", action="store_true", default=os.environ.get("COMFYUI_E2E_SKIP_CLONE") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the Comfy local runtime e2e.")
    if not shutil.which("sudo"):
        raise SystemExit("sudo is required because this host's containerd runtime needs elevated nerdctl.")

    comfy_dir = Path(args.comfy_dir)
    repo_dir = Path(args.repo_dir).resolve()
    ensure_comfyui(comfy_dir, skip_clone=args.skip_clone)

    with tempfile.TemporaryDirectory(prefix="runpod-comfy-local-runtime-") as tmp:
        base_dir = Path(tmp) / "base"
        user_dir = Path(tmp) / "user"
        custom_nodes = base_dir / "custom_nodes"
        custom_nodes.mkdir(parents=True)
        user_dir.mkdir(parents=True)
        (custom_nodes / "comfyui-runpod-agentic").symlink_to(repo_dir, target_is_directory=True)

        port = args.port or default_port()
        cmd = comfy_cmd(args.python, comfy_dir, base_dir, user_dir, port=port)
        proc = subprocess.Popen(cmd, cwd=comfy_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output: list[str] = []
        try:
            wait_for_server(port, proc, output)
            server = f"http://127.0.0.1:{port}"
            try:
                submit_workflow(server, repo_dir / "examples/workflows/api_local_runtime_containerd_up.json", timeout=900)
                services = inspect_project(PROJECT_NAME)
                assert_project_services(services)
                agent = next(service for service in services if service["role"] == "agent")
                ollama_host = env_value(agent["id"], "OLLAMA_HOST")
                if not ollama_host.startswith("http://") or ":11434" not in ollama_host:
                    raise AssertionError(f"Agent OLLAMA_HOST was not resolved to the Compose LLM service: {ollama_host}")
                command_one = file_text(agent["id"], "/workspace/e2e/command-1.txt").strip()
                command_two = file_text(agent["id"], "/workspace/e2e/command-2.txt").strip()
                command_ollama_host = file_text(agent["id"], "/workspace/e2e/ollama-host.txt").strip()
                if command_one != "startup-one" or command_two != "startup-two":
                    raise AssertionError(f"Startup command files were not created correctly: {command_one!r}, {command_two!r}")
                if command_ollama_host != ollama_host:
                    raise AssertionError(f"Startup command saw unexpected OLLAMA_HOST: {command_ollama_host!r} != {ollama_host!r}")
                llm_service = ollama_host.removeprefix("http://").split(":", 1)[0]
                dns = run_runtime(["nerdctl", "exec", agent["id"], "getent", "hosts", llm_service]).stdout.strip()
                if llm_service not in dns:
                    raise AssertionError(f"Agent container could not resolve LLM service {llm_service}: {dns}")
                print(
                    json.dumps(
                        {
                            "services": services,
                            "ollama_host": ollama_host,
                            "startup_files": {
                                "command_1": command_one,
                                "command_2": command_two,
                                "ollama_host": command_ollama_host,
                            },
                            "dns": dns,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            finally:
                submit_workflow(server, repo_dir / "examples/workflows/api_local_runtime_containerd_down.json", timeout=300)
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
    prompt = json.loads(workflow.read_text())
    response = post_json(f"{server}/prompt", {"prompt": prompt, "client_id": uuid.uuid4().hex})
    prompt_id = response["prompt_id"]
    history = wait_history(server, prompt_id, timeout)
    entry = history[prompt_id]
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


def assert_project_services(services: list[dict[str, str]]) -> None:
    if len(services) != 2:
        raise AssertionError(f"Expected 2 local runtime containers, found {len(services)}: {services}")
    if {service["role"] for service in services} != {"agent", "llm"}:
        raise AssertionError(f"Expected agent and llm roles, found: {services}")
    if len({service["network"] for service in services}) != 1:
        raise AssertionError(f"Expected one shared network, found: {services}")


def cleanup_project_containers(services: list[dict[str, str]]) -> None:
    if not services:
        return
    run_runtime(["nerdctl", "rm", "-f", *[service["id"] for service in services]])


def env_value(container_id: str, key: str) -> str:
    inspect = json.loads(run_runtime(["nerdctl", "inspect", container_id]).stdout)[0]
    for entry in inspect.get("Config", {}).get("Env", []):
        if entry.startswith(f"{key}="):
            return entry.split("=", 1)[1]
    return ""


def file_text(container_id: str, path: str) -> str:
    return run_runtime(["nerdctl", "exec", container_id, "cat", path]).stdout


def run_runtime(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(["sudo", *command], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: sudo {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    raise SystemExit(main())
