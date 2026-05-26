from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

from comfyui_runpod_agentic.nodes import AgentNode, BuildContainerNode, DeployNode, RunLocalContainersNode, SSHCommandNode


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a real local agent image, then run a second local deployment from that image.")
    parser.add_argument("--tag", default=f"localhost/crag-build-then-run:{int(time.time())}")
    parser.add_argument("--project-prefix", default=f"crag-build-then-run-{os.getpid()}")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the local build-then-run image e2e test.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")

    build_project = f"{args.project_prefix}-build"
    run_project = f"{args.project_prefix}-run"
    marker = f"crag-built-image-marker-{os.getpid()}"
    old_skip = os.environ.get("CRAG_SKIP_HARNESS_INSTALL")
    os.environ["CRAG_SKIP_HARNESS_INSTALL"] = "1"
    try:
        build_deployment = build_image_deployment(marker)
        build_output = BuildContainerNode().apply(
            build_deployment,
            args.tag,
            container_runtime="nerdctl",
            push_to_docker_hub=False,
            project_name=build_project,
            output_path=f"artifacts/local-runtime/{build_project}.yaml",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
            workflow_graph={},
        )
        image_name = build_output["result"][5]
        if image_name != args.tag:
            raise AssertionError(f"Build Container emitted {image_name!r}, expected {args.tag!r}")
        run_runtime(["nerdctl", "image", "inspect", image_name], args.sudo_runtime)

        run_deployment = run_built_image_deployment(image_name, marker)
        run_output = RunLocalContainersNode().apply(
            run_deployment,
            engine="containerd",
            prompt="verify built image",
            project_name=run_project,
            output_path=f"artifacts/local-runtime/{run_project}.yaml",
            action="apply_and_wait",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
            response_path="/workspace/e2e/build-then-run-image.txt",
            response_timeout_seconds=90,
            reuse_policy="always_create",
            workflow_graph={},
        )
        response = run_output["result"][1].strip()
        if response != marker:
            raise AssertionError(f"Built image marker was not found by the second deployment: {response!r}")
        if f"image: {image_name}" not in run_output["result"][3]:
            raise AssertionError(f"Second deployment compose YAML did not use the built image {image_name!r}")

        print(
            json.dumps(
                {
                    "built_image": image_name,
                    "build_project": build_project,
                    "run_project": run_project,
                    "marker": response,
                    "pushed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        try:
            terminate_project(build_image_deployment(marker), build_project, args.sudo_runtime, args.timeout_seconds)
            terminate_project(run_built_image_deployment(args.tag, marker), run_project, args.sudo_runtime, args.timeout_seconds)
        finally:
            run_runtime(["nerdctl", "rmi", "-f", args.tag], args.sudo_runtime, check=False)
            if old_skip is None:
                os.environ.pop("CRAG_SKIP_HARNESS_INSTALL", None)
            else:
                os.environ["CRAG_SKIP_HARNESS_INSTALL"] = old_skip


def build_image_deployment(marker: str):
    agent = AgentNode().build("Pi", "manual-build", "manual")[0]
    command = SSHCommandNode().build(f"printf '%s\\n' {shlex.quote(marker)} > /crag-built-image-marker", "before_start", "fail")[0]
    return DeployNode().build(agent, commands=command)[0]


def run_built_image_deployment(image_name: str, marker: str):
    agent = AgentNode().build("Pi", "manual-run", "manual", image_name=image_name)[0]
    command = SSHCommandNode().build(
        "\n".join(
            [
                "set -e",
                "mkdir -p /workspace/e2e",
                f"test \"$(cat /crag-built-image-marker)\" = {shlex.quote(marker)}",
                "cat /crag-built-image-marker > /workspace/e2e/build-then-run-image.txt",
            ]
        ),
        "before_start",
        "fail",
    )[0]
    return DeployNode().build(agent, commands=command)[0]


def terminate_project(deployment, project_name: str, use_sudo: bool, timeout_seconds: int) -> None:
    RunLocalContainersNode().apply(
        deployment,
        engine="containerd",
        project_name=project_name,
        output_path=f"artifacts/local-runtime/{project_name}.yaml",
        action="terminate",
        use_sudo=use_sudo,
        timeout_seconds=timeout_seconds,
        response_timeout_seconds=0,
        workflow_graph={},
    )


def containerd_runtime_ready(use_sudo: bool) -> bool:
    return run_runtime(["nerdctl", "info"], use_sudo, check=False).returncode == 0


def run_runtime(command: list[str], use_sudo: bool, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run((["sudo"] if use_sudo else []) + command, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    sys.exit(main())
