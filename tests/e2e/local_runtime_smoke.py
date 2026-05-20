from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from comfyui_runpod_agentic.nodes import (
    AgentNode,
    DeployNode,
    DeployWithContainerdNode,
    LLMServerNode,
    SSHCommandNode,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local container runtime smoke test for CRAG Compose nodes.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-name", default="crag-local-smoke")
    parser.add_argument("--output-path", default="artifacts/local-runtime/smoke-compose.yaml")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the containerd local runtime smoke test.")
    if args.sudo_runtime:
        os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"

    deployment = build_deployment()
    node = DeployWithContainerdNode()

    try:
        up_result_text, response, errors, compose_yaml, saved_path = node.apply(
            deployment,
            prompt="Local runtime smoke test.",
            project_name=args.project_name,
            output_path=args.output_path,
            action="apply",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
        )
        up_result = json.loads(up_result_text)
        if up_result["returncode"] != 0:
            raise AssertionError(f"Containerd apply failed:\n{up_result_text}")
        if errors:
            raise AssertionError(f"Unexpected local runtime apply errors:\n{errors}")
        if "local runtime smoke response" not in response:
            raise AssertionError(f"Did not collect the response file from the agent container:\n{response}")

        services = inspect_project(args.project_name)
        if len(services) != 2:
            raise AssertionError(f"Expected 2 smoke containers, found {len(services)}: {services}")
        roles = {service["role"] for service in services}
        if roles != {"agent", "llm"}:
            raise AssertionError(f"Expected agent and llm roles, found {roles}")
        networks = {service["network"] for service in services}
        if len(networks) != 1:
            raise AssertionError(f"Expected one shared project network, found {networks}")
        agent_env = inspect_env(next(service["id"] for service in services if service["role"] == "agent"))
        ollama_host = next((value.split("=", 1)[1] for value in agent_env if value.startswith("OLLAMA_HOST=")), "")
        if not ollama_host.startswith("http://") or ":11434" not in ollama_host:
            raise AssertionError(f"Agent OLLAMA_HOST was not resolved to the local service endpoint: {ollama_host}")
        llm_service = ollama_host.removeprefix("http://").split(":", 1)[0]
        agent_id = next(service["id"] for service in services if service["role"] == "agent")
        dns = run(["nerdctl", "exec", agent_id, "getent", "hosts", llm_service]).stdout.strip()
        if llm_service not in dns:
            raise AssertionError(f"Agent container could not resolve LLM service {llm_service}: {dns}")

        print(json.dumps({"compose_path": saved_path, "services": services, "ollama_host": ollama_host, "dns": dns, "response": response, "compose_yaml_bytes": len(compose_yaml.encode())}, indent=2, sort_keys=True))
        return 0
    finally:
        node.apply(
            deployment,
            prompt="Local runtime smoke test.",
            project_name=args.project_name,
            output_path=args.output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
        )


def build_deployment():
    llm = LLMServerNode().build("Ollama", "smoke", "own_pod", "none")[0]
    agent = AgentNode().build("Pi", "smoke", "manual", "/workspace", llm=llm)[0]
    command = SSHCommandNode().build("mkdir -p /workspace/e2e && printf 'local runtime smoke response\\n' > /workspace/e2e/agent-skill-report.txt", "before_start", 10, "fail")[0]
    return DeployNode().build(agent, gpu_count=0, expose_public_ip=False, reuse_policy="always_create", commands=command)[0]


def inspect_project(project_name: str) -> list[dict[str, str]]:
    completed = run(["nerdctl", "ps", "--format", "json"])
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
        networks = inspect.get("NetworkSettings", {}).get("Networks", {})
        services.append(
            {
                "id": container_id,
                "name": str(names),
                "role": labels.get("comfyui-runpod-agentic.role", ""),
                "network": next(iter(networks), ""),
            }
        )
    return services


def inspect_env(container_id: str) -> list[str]:
    data = json.loads(run(["nerdctl", "inspect", container_id]).stdout)[0]
    return data.get("Config", {}).get("Env", [])


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    if os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1" and command[0] in {"docker", "podman", "nerdctl"}:
        command = ["sudo", *command]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    sys.exit(main())
