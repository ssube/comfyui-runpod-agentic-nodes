from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .nodes import AgentNode, DeployNode, KeepAliveNode, SSHAccessNode, SSHCommandNode, with_terminal_options
from .runner import RunpodRunner


def build_smoke_deployment(gpu_type_id: str, gpu_count: int, keepalive_minutes: int, cloud_type: str):
    command = SSHCommandNode().build(
        "echo crag-live-smoke && python --version && pwd",
        "before_start",
        "fail",
    )[0]
    keep_alive = KeepAliveNode().build("time", "stop", keepalive_minutes, "minutes", 0, 0.0, 0)[0]
    ssh_access = SSHAccessNode().build("runpod_proxy", "root", "~/.ssh/id_ed25519", "", 22, False)[0]
    agent = AgentNode().build("Pi", "manual-smoke", "manual", "/workspace", node_id="live-smoke-agent")[0]
    deployment = DeployNode().build(agent, commands=command, keep_alive=keep_alive, node_id="live-smoke-pod")[0]
    return with_terminal_options(deployment, gpu_type_id=gpu_type_id, gpu_count=gpu_count, cloud_type=cloud_type, container_disk_gb=20, volume_gb=0, expose_public_ip=True, reuse_policy="always_create", ssh_access=ssh_access)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a minimal real Runpod pod and execute a smoke command.")
    parser.add_argument("--gpu-type-id", default="NVIDIA RTX A4000")
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY"], default="SECURE")
    parser.add_argument("--keepalive-minutes", type=int, default=15)
    parser.add_argument("--mode", choices=["plan", "apply"], default="apply")
    parser.add_argument("--output", default="user/runpod-agentic/live-smoke-result.json")
    parser.add_argument("--cleanup", choices=["none", "stop", "terminate"], default="terminate")
    args = parser.parse_args()

    deployment = build_smoke_deployment(args.gpu_type_id, args.gpu_count, args.keepalive_minutes, args.cloud_type)
    if args.mode == "plan":
        from .planner import Planner

        result = Planner().build(deployment, mode="plan", workflow_graph={"live_smoke": True}).to_dict()
    else:
        runner = RunpodRunner()
        result = runner.run(deployment, mode="apply", workflow_graph={"live_smoke": True, "timestamp": time.time()}, on_error="terminate_created")
        cleanup_pods(runner, result, args.cleanup)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cleanup_pods(runner: RunpodRunner, result: dict, cleanup: str) -> None:
    if cleanup == "none":
        return
    pod_ids = list((result.get("pods") or {}).values())
    for pod_id in pod_ids:
        if cleanup == "terminate":
            runner.runpod_client.terminate_pod(pod_id)
        else:
            runner.runpod_client.stop_pod(pod_id)


if __name__ == "__main__":
    raise SystemExit(main())
