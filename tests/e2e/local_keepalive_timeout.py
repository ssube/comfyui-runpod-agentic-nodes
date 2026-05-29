from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from comfyui_runpod_agentic.nodes import AgentNode, DeployNode, KeepAliveNode, RunLocalContainersNode, SSHCommandNode


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify local keep-alive timeouts stop real containerd containers.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-name", default="crag-local-keepalive-timeout")
    parser.add_argument("--output-path", default="artifacts/local-runtime/keepalive-timeout-compose.yaml")
    parser.add_argument("--timeout-seconds", type=int, default=5)
    parser.add_argument("--shutdown-timeout-seconds", type=int, default=60)
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the containerd local keep-alive timeout test.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")
    if args.sudo_runtime:
        os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"

    deployment = build_deployment(args.timeout_seconds)
    node = RunLocalContainersNode()

    try:
        result_text, _response, errors, _compose_yaml, saved_path, _image = node.apply(
            deployment,
            engine=args.engine,
            prompt="Local keep-alive timeout smoke test.",
            project_name=args.project_name,
            output_path=args.output_path,
            action="apply",
            use_sudo=args.sudo_runtime,
            timeout_seconds=120,
            response_path="",
            response_timeout_seconds=0,
            reuse_policy="always_create",
        )
        result = json.loads(result_text)
        if result["returncode"] != 0:
            raise AssertionError(f"Containerd apply failed:\n{result_text}\n{errors}")
        keep_alive = result.get("keep_alive") or {}
        if keep_alive.get("returncode") != 0 or f"in {args.timeout_seconds} seconds" not in keep_alive.get("stdout", ""):
            raise AssertionError(f"Local keep-alive scheduler did not report the expected timeout:\n{result_text}")

        running = inspect_project(args.project_name, all_containers=False)
        if not running:
            raise AssertionError(f"Expected at least one running container immediately after apply, found: {running}")
        agent_id = next(service["id"] for service in running if service["role"] == "agent")
        startup_marker = wait_for_startup_marker(args.project_name, agent_id, args.shutdown_timeout_seconds)
        if startup_marker != "startup-before-timeout":
            raise AssertionError(f"Startup command marker was not present before keep-alive shutdown: {startup_marker!r}")

        started = time.monotonic()
        stopped_services = wait_for_no_running_containers(args.project_name, args.shutdown_timeout_seconds)
        elapsed = time.monotonic() - started
        if elapsed < max(0, args.timeout_seconds - 1):
            raise AssertionError(f"Local keep-alive stopped containers too early after {elapsed:.2f}s")

        print(
            json.dumps(
                {
                    "compose_path": saved_path,
                    "elapsed_shutdown_seconds": round(elapsed, 2),
                    "initial_services": running,
                    "startup_marker": startup_marker,
                    "stopped_services": stopped_services,
                    "keep_alive": keep_alive,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        node.apply(
            deployment,
            prompt="Local keep-alive timeout smoke test.",
            project_name=args.project_name,
            output_path=args.output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=120,
            response_timeout_seconds=0,
        )


def build_deployment(timeout_seconds: int):
    keep_alive = KeepAliveNode().build("time", "stop", timeout_seconds, "seconds", 0, 0.0, 0, "server_side")[0]
    agent = AgentNode().build("Pi", "keepalive-timeout", "manual", "/workspace", node_id="local-keepalive-agent")[0]
    command = SSHCommandNode().build(
        "printf startup-before-timeout > /workspace/.crag-startup-marker",
        "before_start",
        "fail",
    )[0]
    return DeployNode().build(agent, commands=command, keep_alive=keep_alive, node_id="local-keepalive-deploy")[0]


def wait_for_no_running_containers(project_name: str, timeout_seconds: int) -> list[dict[str, str]]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        running = inspect_project(project_name, all_containers=False)
        if not running:
            return inspect_project(project_name, all_containers=True)
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for local keep-alive to stop project {project_name}: {inspect_project(project_name, all_containers=False)}")


def wait_for_startup_marker(project_name: str, container_id: str, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not inspect_project(project_name, all_containers=False):
            raise AssertionError("Container stopped before the startup command marker was written.")
        marker = read_container_file(container_id, "/workspace/.crag-startup-marker")
        if marker is not None:
            return marker.strip()
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for startup command marker before keep-alive shutdown.")


def read_container_file(container_id: str, path: str) -> str | None:
    command = ["nerdctl", "exec", container_id, "cat", path]
    if os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1":
        command = ["sudo", *command]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return None
    return completed.stdout


def containerd_runtime_ready(use_sudo: bool) -> bool:
    command = ["nerdctl", "info"]
    if use_sudo:
        command = ["sudo", *command]
    return subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0


def inspect_project(project_name: str, *, all_containers: bool) -> list[dict[str, str]]:
    command = ["nerdctl", "ps", "--format", "json"]
    if all_containers:
        command.insert(2, "-a")
    completed = run(command)
    services = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        names = data.get("Names") or data.get("Name") or ""
        if not str(names).startswith(f"{project_name}-"):
            continue
        container_id = data["ID"]
        inspect = json.loads(run(["nerdctl", "inspect", container_id]).stdout)[0]
        labels = inspect.get("Config", {}).get("Labels", {})
        services.append(
            {
                "id": container_id,
                "name": str(names),
                "role": labels.get("comfyui-runpod-agentic.role", ""),
                "status": str(data.get("Status") or data.get("State") or ""),
            }
        )
    return services


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    if os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1" and command[0] in {"docker", "podman", "nerdctl"}:
        command = ["sudo", *command]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    sys.exit(main())
