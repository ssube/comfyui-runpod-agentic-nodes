from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from comfyui_runpod_agentic.nodes import AgentNode, DeployNode, RunLocalContainersNode, SSHCommandNode

HARNESSES = {
    "Codex": "codex",
    "Claude": "claude",
    "OpenCode": "opencode",
    "Hermes": "hermes",
    "Pi": "pi",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install each supported harness in a fresh local container and collect help/version output.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-prefix", default=f"crag-harness-install-{os.getpid()}")
    parser.add_argument("--output-dir", default="artifacts/local-runtime/harness-installs")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--harness", action="append", choices=sorted(HARNESSES), help="Limit the run to one or more harness display names.")
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the live harness install e2e.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")
    if args.sudo_runtime:
        os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"

    selected = args.harness or list(HARNESSES)
    results = [run_harness(args, harness, HARNESSES[harness]) for harness in selected]
    print(json.dumps({"harnesses": results}, indent=2, sort_keys=True))
    return 0


def run_harness(args: argparse.Namespace, harness: str, binary: str) -> dict[str, object]:
    project_name = f"{args.project_prefix}-{binary}"
    output_path = f"{args.output_dir}/{binary}.yaml"
    report_path = "/workspace/.runpod_agentic/response.txt"
    deployment = build_deployment(harness, binary, report_path)
    node = RunLocalContainersNode()
    try:
        result_text, response, errors, _compose_yaml, saved_path = node.apply(
            deployment,
            engine=args.engine,
            prompt=f"Install {harness} harness and collect CLI metadata.",
            project_name=project_name,
            output_path=output_path,
            action="apply_and_wait",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
            response_path=report_path,
            response_timeout_seconds=args.timeout_seconds,
            reuse_policy="always_create",
        )
        result = json.loads(result_text)
        if result["returncode"] != 0:
            raise AssertionError(f"{harness} apply_and_wait failed:\n{result_text}\n{errors}")
        try:
            report = parse_report(response)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{harness} did not return a probe report. Raw response:\n{response}\nErrors:\n{errors}") from exc
        if report["help_returncode"] != 0 or not report["help_output"].strip():
            raise AssertionError(f"{harness} --help did not produce usable output:\n{response}")
        if report["version_returncode"] != 0 or not report["version_output"].strip():
            raise AssertionError(f"{harness} did not produce usable version output:\n{response}")
        return {
            "harness": harness,
            "binary": binary,
            "compose_path": saved_path,
            "help_excerpt": report["help_output"].strip()[:2000],
            "version": report["version_output"].strip()[:500],
        }
    finally:
        node.apply(
            deployment,
            prompt=f"Terminate {harness} harness install test.",
            project_name=project_name,
            output_path=output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=120,
            response_timeout_seconds=0,
        )


def build_deployment(harness: str, binary: str, report_path: str):
    agent = AgentNode().build(
        harness,
        f"{binary}-install-smoke",
        "manual",
        "/workspace",
        system_prompt=f"Live install verification for {harness}.",
        node_id=f"install-{binary}",
    )[0]
    command = SSHCommandNode().build(harness_probe_command(binary, report_path), "after_ready", "fail")[0]
    return DeployNode().build(agent, commands=command, node_id=f"deploy-{binary}")[0]


def harness_probe_command(binary: str, report_path: str) -> str:
    return f"""set -e
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.cargo/bin:$PATH"
mkdir -p /workspace/e2e "$(dirname {report_path!r})"
help_file=/workspace/e2e/{binary}-help.txt
version_file=/workspace/e2e/{binary}-version.txt
set +e
{binary} --help >"$help_file" 2>&1
help_status=$?
{binary} --version >"$version_file" 2>&1
version_status=$?
if [ "$version_status" -ne 0 ] || [ ! -s "$version_file" ]; then
  {binary} version >"$version_file" 2>&1
  version_status=$?
fi
set -e
export CRAG_HELP_STATUS="$help_status"
export CRAG_VERSION_STATUS="$version_status"
python3 - <<'PY'
import os
import json
from pathlib import Path

help_file = Path("/workspace/e2e/{binary}-help.txt")
version_file = Path("/workspace/e2e/{binary}-version.txt")
Path({report_path!r}).write_text(json.dumps({{
    "binary": {binary!r},
    "complete": "[crag-agent] complete status=0",
    "help_returncode": int(os.environ["CRAG_HELP_STATUS"]),
    "help_output": help_file.read_text(errors="replace"),
    "version_returncode": int(os.environ["CRAG_VERSION_STATUS"]),
    "version_output": version_file.read_text(errors="replace"),
}}, indent=2, sort_keys=True))
PY
"""


def parse_report(response: str) -> dict:
    stripped = response.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(stripped):
            if char != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and {"binary", "help_returncode", "version_returncode"}.issubset(candidate):
                return candidate
        raise


def containerd_runtime_ready(use_sudo: bool) -> bool:
    command = ["nerdctl", "info"]
    if use_sudo:
        command = ["sudo", *command]
    return subprocess.run(command, capture_output=True, text=True, check=False).returncode == 0


if __name__ == "__main__":
    sys.exit(main())
