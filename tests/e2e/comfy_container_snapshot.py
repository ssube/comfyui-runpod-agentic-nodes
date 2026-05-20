from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from comfy_cpu_smoke import comfy_cmd, default_port, ensure_comfyui, wait_for_server


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a ComfyUI API e2e test for the Build Container snapshot plan workflow.")
    parser.add_argument("--comfy-dir", default=os.environ.get("COMFYUI_E2E_DIR", "/tmp/comfyui-runpod-e2e"))
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMFYUI_CONTAINER_SNAPSHOT_E2E_PORT", "0")))
    parser.add_argument("--skip-clone", action="store_true", default=os.environ.get("COMFYUI_E2E_SKIP_CLONE") == "1")
    args = parser.parse_args()

    comfy_dir = Path(args.comfy_dir)
    repo_dir = Path(args.repo_dir).resolve()
    ensure_comfyui(comfy_dir, skip_clone=args.skip_clone)

    with tempfile.TemporaryDirectory(prefix="runpod-comfy-container-snapshot-") as tmp:
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
            workflow_path = repo_dir / "examples/workflows/api_container_snapshot_plan.json"
            submit_workflow(server, workflow_path, timeout=120)
            compose_path = comfy_dir / "artifacts/local-runtime/container-snapshot-compose.yaml"
            if not compose_path.exists():
                raise AssertionError(f"Snapshot workflow did not write compose YAML: {compose_path}")
            compose_yaml = compose_path.read_text()
            required_snippets = (
                "docker.io/example/crag-agent-base:dev",
                "commit",
                "container_id=",
                "image_tag=docker.io/example/crag-agent-base:dev",
                "container snapshots",
                "npm install -g @earendil-works/pi-coding-agent",
                "ca-certificates curl git jq gnupg",
            )
            missing = [snippet for snippet in required_snippets if snippet not in compose_yaml]
            if missing:
                raise AssertionError(f"Generated compose YAML was missing snapshot snippets {missing}:\n{compose_yaml}")
            if "push \\\"$image_tag\\\"" in compose_yaml:
                raise AssertionError(f"Example snapshot workflow should not push by default:\n{compose_yaml}")
            print(
                json.dumps(
                    {
                        "compose_path": str(compose_path),
                        "compose_excerpt": compose_yaml[:1200],
                        "snapshot_command_present": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
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
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
