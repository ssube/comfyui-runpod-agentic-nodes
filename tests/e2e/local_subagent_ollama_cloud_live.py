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
    LLMApiNode,
    RunLocalContainersNode,
    SubagentNode,
)

MARKER = "CRAG_SUBAGENT_LIVE_OK"
PROJECT_NAME = "crag-local-subagent-ollama-cloud"
RESPONSE_PATH = "/workspace/.runpod_agentic/response.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live local Pi + Ollama Cloud e2e that delegates to a CRAG subagent.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-name", default=PROJECT_NAME)
    parser.add_argument("--output-path", default="artifacts/local-runtime/subagent-ollama-cloud-compose.yaml")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the live subagent Ollama Cloud e2e.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")
    if not has_ollama_key():
        raise SystemExit("OLLAMA_API_KEY is required in .env.d/ollama.env or the process environment.")
    if args.sudo_runtime:
        os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"

    deployment = build_deployment()
    node = RunLocalContainersNode()
    try:
        result_text, response, errors, compose_yaml, saved_path = node.apply(
            deployment,
            engine=args.engine,
            prompt=(
                "Use the crag_delegate_subagent tool with name=reviewer and task='Return the required marker'. "
                f"Reply with the exact token {MARKER} after the subagent responds."
            ),
            project_name=args.project_name,
            output_path=args.output_path,
            action="apply_and_wait",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
            response_path=RESPONSE_PATH,
            response_timeout_seconds=args.timeout_seconds,
            reuse_policy="always_create",
        )
        result = json.loads(result_text)
        if result["returncode"] != 0:
            raise AssertionError(f"Subagent Ollama Cloud apply failed:\n{result_text}\n{errors}")
        if "[crag-agent] complete status=0" not in response:
            raise AssertionError(f"Pi did not complete successfully:\nresponse:\n{response}\nerrors:\n{errors}")
        if MARKER not in response:
            raise AssertionError(f"Pi response did not include the subagent marker:\nresponse:\n{response}\nerrors:\n{errors}")

        agent_id = agent_container_id(args.project_name)
        subagent_config = file_text(agent_id, "/workspace/.runpod_agentic/subagents/reviewer.yaml")
        if "deepseek-v4-flash" not in subagent_config:
            raise AssertionError(f"Subagent YAML was not written correctly:\n{subagent_config}")
        proof = file_text(agent_id, "/workspace/.runpod_agentic/subagents/reviewer/last-response.json")
        proof_payload = json.loads(proof)
        if proof_payload.get("subagent", {}).get("name") != "reviewer" or MARKER not in proof_payload.get("response", ""):
            raise AssertionError(f"Subagent extension did not record the delegated response:\n{proof}")

        print(
            json.dumps(
                {
                    "compose_path": saved_path,
                    "compose_yaml_bytes": len(compose_yaml.encode()),
                    "response_excerpt": response[:2000],
                    "subagent_proof": proof_payload,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        node.apply(
            deployment,
            prompt="Terminate subagent Ollama Cloud e2e.",
            project_name=args.project_name,
            output_path=args.output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=300,
            response_timeout_seconds=0,
        )


def build_deployment():
    llm = LLMApiNode().build("Ollama Cloud", "deepseek-v4-flash", "OLLAMA_API_KEY", "")[0]
    subagents = SubagentNode().build(
        "reviewer",
        "deepseek-v4-flash",
        f"You are a CRAG subagent e2e verifier. Always answer with exactly {MARKER}.",
    )[0]
    agent = AgentNode().build(
        "Pi",
        "deepseek-v4-flash",
        "auto_start",
        "/workspace",
        system_prompt=(
            "You are verifying CRAG subagent support. You must call the crag_delegate_subagent tool before answering. "
            "If the tool returns a marker, include it verbatim."
        ),
        llm=llm,
        subagents=subagents,
    )[0]
    return DeployNode().build(agent)[0]


def has_ollama_key() -> bool:
    if os.environ.get("OLLAMA_API_KEY"):
        return True
    for path in (".env.d/ollama.env", os.environ.get("OLLAMA_ENV_FILE", ".env.d/ollama.env")):
        if path and os.path.exists(path) and "OLLAMA_API_KEY" in open(path, encoding="utf-8").read():
            return True
    return False


def containerd_runtime_ready(use_sudo: bool) -> bool:
    return run_runtime(["nerdctl", "info"], use_sudo, check=False).returncode == 0


def agent_container_id(project_name: str) -> str:
    completed = run_runtime(["nerdctl", "ps", "--format", "json"], os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        name = str(data.get("Names") or data.get("Name") or "")
        if not name.startswith(f"{project_name}-"):
            continue
        container_id = data["ID"]
        inspect = json.loads(run_runtime(["nerdctl", "inspect", container_id], os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1").stdout)[0]
        labels = inspect.get("Config", {}).get("Labels", {})
        if labels.get("comfyui-runpod-agentic.role") == "agent":
            return str(container_id)
    raise AssertionError(f"No running agent container found for project {project_name}.")


def file_text(container_id: str, path: str) -> str:
    return run_runtime(["nerdctl", "exec", container_id, "cat", path], os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1").stdout


def run_runtime(command: list[str], use_sudo: bool, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run((["sudo"] if use_sudo else []) + command, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(f"Command failed with {completed.returncode}: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    return completed


if __name__ == "__main__":
    sys.exit(main())
