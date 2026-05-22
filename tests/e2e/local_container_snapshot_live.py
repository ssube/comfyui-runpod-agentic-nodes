from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from comfyui_runpod_agentic.setup_commands import container_snapshot_command


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live local container snapshot test with nerdctl commit.")
    parser.add_argument("--image", default="ubuntu:24.04")
    parser.add_argument("--tag", default=f"localhost/crag-live-snapshot:{int(time.time())}")
    parser.add_argument("--marker", default="crag-live-snapshot-marker")
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the live container snapshot test.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")

    source_container = ""
    snapshot_container = ""
    try:
        source_container = run_runtime(["nerdctl", "run", "-d", "--name", f"crag-snapshot-src-{os.getpid()}", args.image, "sleep", "300"], args.sudo_runtime).stdout.strip()
        run_runtime(["nerdctl", "exec", source_container, "sh", "-lc", f"printf '%s' {shell_quote(args.marker)} > /crag-snapshot-marker"], args.sudo_runtime)

        snapshot_script = container_snapshot_command(args.tag, "nerdctl", False, "DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN")
        run_snapshot_script(snapshot_script, source_container, args.sudo_runtime)
        run_runtime(["nerdctl", "image", "inspect", args.tag], args.sudo_runtime)

        snapshot_container = run_runtime(["nerdctl", "run", "--rm", args.tag, "cat", "/crag-snapshot-marker"], args.sudo_runtime).stdout.strip()
        if snapshot_container != args.marker:
            raise AssertionError(f"Snapshot image did not preserve marker file: {snapshot_container!r}")

        print(json.dumps({"image": args.image, "snapshot_tag": args.tag, "source_container": source_container, "marker": snapshot_container}, indent=2, sort_keys=True))
        return 0
    finally:
        if source_container:
            subprocess.run(runtime_command(["nerdctl", "rm", "-f", source_container], args.sudo_runtime), capture_output=True, text=True, check=False)
        subprocess.run(runtime_command(["nerdctl", "rmi", "-f", args.tag], args.sudo_runtime), capture_output=True, text=True, check=False)


def containerd_runtime_ready(use_sudo: bool) -> bool:
    return subprocess.run(runtime_command(["nerdctl", "info"], use_sudo), capture_output=True, text=True, check=False).returncode == 0


def run_snapshot_script(script: str, container_id: str, use_sudo: bool) -> None:
    command = ["bash", "-lc", script]
    env = {**os.environ, "HOST_CONTAINER_ID": container_id}
    if use_sudo:
        command = ["sudo", "env", f"HOST_CONTAINER_ID={container_id}", "bash", "-lc", script]
        env = None
    completed = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"Snapshot command failed with {completed.returncode}:\n{completed.stdout}\n{completed.stderr}")


def run_runtime(command: list[str], use_sudo: bool) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(runtime_command(command, use_sudo), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: {' '.join(runtime_command(command, use_sudo))}\n{completed.stdout}\n{completed.stderr}")
    return completed


def runtime_command(command: list[str], use_sudo: bool) -> list[str]:
    return ["sudo", *command] if use_sudo else command


def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


if __name__ == "__main__":
    sys.exit(main())
