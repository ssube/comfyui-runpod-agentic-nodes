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

PROJECT_NAME = "crag-local-ollama-deepseek-e2e"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real local-runtime Pi + Ollama Cloud DeepSeek e2e through ComfyUI.")
    parser.add_argument("--comfy-dir", default=os.environ.get("COMFYUI_E2E_DIR", "/tmp/comfyui-runpod-e2e"))
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMFYUI_LOCAL_OLLAMA_E2E_PORT", "0")))
    parser.add_argument("--skip-clone", action="store_true", default=os.environ.get("COMFYUI_E2E_SKIP_CLONE") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the local Ollama Cloud e2e.")
    if not shutil.which("sudo"):
        raise SystemExit("sudo is required because this host's containerd runtime needs elevated nerdctl.")

    repo_dir = Path(args.repo_dir).resolve()
    if not (repo_dir / ".env.d/ollama.env").exists() and not os.environ.get("OLLAMA_API_KEY"):
        raise SystemExit("OLLAMA_API_KEY is required in .env.d/ollama.env or the process environment.")

    comfy_dir = Path(args.comfy_dir)
    ensure_comfyui(comfy_dir, skip_clone=args.skip_clone)

    with tempfile.TemporaryDirectory(prefix="runpod-comfy-ollama-deepseek-") as tmp:
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
                workflow_path = repo_dir / "examples/workflows/api_local_ollama_cloud_deepseek_agent_up.json"
                entry = submit_workflow(server, workflow_path, timeout=1800)
                response = node_output_text(entry, "7", "response")
                errors = node_output_text(entry, "7", "errors")
                services = wait_for_services(PROJECT_NAME, {"agent"}, timeout=1200)
                agent = next(service for service in services if service["role"] == "agent")
                runtime_response = file_text(agent["id"], "/workspace/.runpod_agentic/response.txt")
                models = json.loads(file_text(agent["id"], "/workspace/.runpod_agentic/harness/pi/models.json"))
                providers = json.loads(file_text(agent["id"], "/workspace/.runpod_agentic/harness/pi/providers.json"))
                skills = file_text(agent["id"], "/workspace/e2e/skills.txt")
                pi_version = file_text(agent["id"], "/workspace/e2e/pi-version.txt")
                if "deepseek-v4-flash" not in json.dumps(models):
                    raise AssertionError(f"Pi models.json does not contain deepseek-v4-flash:\n{json.dumps(models, indent=2)}")
                if "OLLAMA_CLOUD_API_KEY" not in json.dumps(providers):
                    raise AssertionError(f"Pi providers.json does not reference OLLAMA_CLOUD_API_KEY:\n{json.dumps(providers, indent=2)}")
                if "SKILL.md" not in skills:
                    raise AssertionError(f"Superpowers skills were not visible to the agent:\n{skills[:1000]}")
                if "[crag-agent] complete status=0" not in runtime_response:
                    raise AssertionError(f"Pi did not complete successfully:\nresponse:\n{runtime_response}\nerrors:\n{errors}")
                if "SKILL.md" not in runtime_response and "skill" not in runtime_response.lower():
                    raise AssertionError(f"LLM response did not mention skills:\n{runtime_response}")
                second_workflow = json.loads(workflow_path.read_text())
                second_prompt = "Reply with the exact token CRAG_SECOND_PROMPT_OK, then list one skill file available on disk."
                second_workflow["7"]["inputs"]["prompt"] = second_prompt
                second_entry = submit_workflow_payload(server, second_workflow, timeout=1800)
                second_services = wait_for_services(PROJECT_NAME, {"agent"}, timeout=120)
                second_agent = next(service for service in second_services if service["role"] == "agent")
                second_response = node_output_text(second_entry, "7", "response") or file_text(second_agent["id"], "/workspace/.runpod_agentic/response.txt")
                if second_agent["id"] != agent["id"]:
                    raise AssertionError(f"Expected second apply to reuse agent container {agent['id']}, got {second_agent['id']}.")
                if "CRAG_SECOND_PROMPT_OK" not in second_response:
                    raise AssertionError(f"Second prompt response did not prove relaunch:\n{second_response}")
                print(
                    json.dumps(
                        {
                            "services": services,
                            "second_services": second_services,
                            "pi_version": pi_version.strip(),
                            "response_excerpt": response[:1600] or runtime_response[:1600],
                            "second_response_excerpt": second_response[:800],
                            "errors_excerpt": errors[:1600],
                            "skill_lines": skills.splitlines()[:10],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            finally:
                try:
                    submit_workflow(server, repo_dir / "examples/workflows/api_local_ollama_cloud_deepseek_agent_down.json", timeout=600)
                except Exception:
                    cleanup_project_containers(inspect_project(PROJECT_NAME))
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
    return submit_workflow_payload(server, json.loads(workflow.read_text()), timeout=timeout, source=str(workflow))


def submit_workflow_payload(server: str, workflow: dict, *, timeout: int, source: str = "<workflow>") -> dict:
    response = post_json(f"{server}/prompt", {"prompt": workflow, "client_id": uuid.uuid4().hex})
    prompt_id = response["prompt_id"]
    entry = wait_history(server, prompt_id, timeout)[prompt_id]
    status = entry.get("status", {})
    messages = status.get("messages") or []
    if status.get("completed") is False or any(message and message[0] == "execution_error" for message in messages):
        raise AssertionError(f"Workflow {source} failed:\n{json.dumps(entry, indent=2, sort_keys=True)}")
    return entry


def node_output_text(entry: dict, node_id: str, name: str) -> str:
    outputs = entry.get("outputs", {}).get(node_id, {})
    value = outputs.get(name)
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value or "")


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
        services.append({"id": container_id, "name": name, "role": labels.get("comfyui-runpod-agentic.role", "")})
    return services


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
