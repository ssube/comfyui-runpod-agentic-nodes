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
from pathlib import Path

EXPECTED_NODES = {
    "Agent",
    "Browser",
    "LLMServer",
    "LLMApi",
    "MCPServer",
    "Skill",
    "SkillFramework",
    "RemoteSQLDatabase",
    "LocalSQLDatabase",
    "VectorDatabase",
    "NetworkStorage",
    "S3Storage",
    "SSHCommand",
    "Package",
    "LanguageRuntime",
    "BuildContainer",
    "KeepAlive",
    "Deploy",
    "RunOnRunpod",
    "StartupScript",
    "ComposeYAML",
    "RunLocalContainers",
    "Logs",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a CPU-only ComfyUI custom node e2e smoke test.")
    parser.add_argument("--comfy-dir", default=os.environ.get("COMFYUI_E2E_DIR", "/tmp/comfyui-runpod-e2e"))
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMFYUI_E2E_PORT", "0")))
    parser.add_argument("--install-deps", action="store_true", default=os.environ.get("COMFYUI_E2E_INSTALL_DEPS") == "1")
    parser.add_argument("--skip-clone", action="store_true", default=os.environ.get("COMFYUI_E2E_SKIP_CLONE") == "1")
    args = parser.parse_args()

    comfy_dir = Path(args.comfy_dir)
    repo_dir = Path(args.repo_dir).resolve()
    ensure_comfyui(comfy_dir, skip_clone=args.skip_clone)
    if args.install_deps:
        run([args.python, "-m", "pip", "install", "-r", str(comfy_dir / "requirements.txt")], cwd=repo_dir)

    with tempfile.TemporaryDirectory(prefix="runpod-comfy-e2e-") as tmp:
        base_dir = Path(tmp) / "base"
        user_dir = Path(tmp) / "user"
        custom_nodes = base_dir / "custom_nodes"
        custom_nodes.mkdir(parents=True)
        user_dir.mkdir(parents=True)
        link = custom_nodes / "comfyui-runpod-agentic"
        link.symlink_to(repo_dir, target_is_directory=True)

        quick_test(args.python, comfy_dir, base_dir, user_dir)
        port = args.port or default_port()
        server_test(args.python, comfy_dir, base_dir, user_dir, port)

    print("ComfyUI CPU e2e smoke passed")
    return 0


def ensure_comfyui(comfy_dir: Path, *, skip_clone: bool) -> None:
    if (comfy_dir / "main.py").exists():
        return
    if skip_clone:
        raise SystemExit(f"ComfyUI checkout missing: {comfy_dir}")
    comfy_dir.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--depth", "1", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)], cwd=comfy_dir.parent)


def quick_test(python: str, comfy_dir: Path, base_dir: Path, user_dir: Path) -> None:
    cmd = comfy_cmd(python, comfy_dir, base_dir, user_dir, port=0)
    cmd.extend(["--quick-test-for-ci", "--dont-print-server"])
    completed = subprocess.run(cmd, cwd=comfy_dir, capture_output=True, text=True, timeout=120, check=False)
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise AssertionError(f"ComfyUI quick load failed with {completed.returncode}\n{output}")
    assert_loaded(output)


def server_test(python: str, comfy_dir: Path, base_dir: Path, user_dir: Path, port: int) -> None:
    cmd = comfy_cmd(python, comfy_dir, base_dir, user_dir, port=port)
    proc = subprocess.Popen(cmd, cwd=comfy_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output: list[str] = []
    try:
        wait_for_server(port, proc, output)
        data = fetch_json(f"http://127.0.0.1:{port}/object_info")
        missing = sorted(EXPECTED_NODES.difference(data))
        if missing:
            raise AssertionError(f"ComfyUI object_info missing nodes: {missing}")
        assert data["RunOnRunpod"]["output_node"] is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
    assert_no_import_failure("".join(output))


def comfy_cmd(python: str, comfy_dir: Path, base_dir: Path, user_dir: Path, *, port: int) -> list[str]:
    cmd = [
        python,
        str(comfy_dir / "main.py"),
        "--cpu",
        "--listen",
        "127.0.0.1",
        "--base-directory",
        str(base_dir),
        "--user-directory",
        str(user_dir),
        "--disable-api-nodes",
        "--whitelist-custom-nodes",
        "comfyui-runpod-agentic",
        "--log-stdout",
    ]
    if port:
        cmd.extend(["--port", str(port)])
    return cmd


def wait_for_server(port: int, proc: subprocess.Popen[str], output: list[str]) -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            if proc.stdout:
                output.append(proc.stdout.read())
            raise AssertionError(f"ComfyUI server exited early with {proc.returncode}\n{''.join(output)}")
        if proc.stdout:
            drain_stdout(proc, output)
        try:
            fetch_json(f"http://127.0.0.1:{port}/object_info")
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(1)
    raise AssertionError(f"ComfyUI server did not become ready\n{''.join(output)}")


def drain_stdout(proc: subprocess.Popen[str], output: list[str]) -> None:
    import select

    assert proc.stdout is not None
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], 0)
        if not ready:
            return
        line = proc.stdout.readline()
        if not line:
            return
        output.append(line)


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def default_port() -> int:
    return 18000 + (os.getpid() % 20000)


def assert_loaded(output: str) -> None:
    if "comfyui-runpod-agentic" not in output and "ComfyUI-Runpod-Agentic" not in output:
        raise AssertionError(f"Custom node load log did not mention this package\n{output}")
    assert_no_import_failure(output)


def assert_no_import_failure(output: str) -> None:
    failed_markers = ("Cannot import", "Traceback", "ImportError", "ModuleNotFoundError")
    if any(marker in output for marker in failed_markers):
        raise AssertionError(f"Custom node load output contains an import failure\n{output}")


def run(cmd: list[str], cwd: Path) -> None:
    if shutil.which(cmd[0]) is None and not Path(cmd[0]).exists():
        raise SystemExit(f"Command not found: {cmd[0]}")
    subprocess.run(cmd, cwd=cwd, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
