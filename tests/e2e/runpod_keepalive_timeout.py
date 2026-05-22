from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from comfyui_runpod_agentic.keepalive_timer import schedule_runpod_lifecycle
from comfyui_runpod_agentic.nodes import (
    AgentNode,
    DeployNode,
    KeepAliveNode,
    with_terminal_options,
)
from comfyui_runpod_agentic.planner import Planner
from comfyui_runpod_agentic.runpod_client import RunpodClient, RunpodClientError

STOPPED_STATUSES = {"EXITED", "STOPPED", "TERMINATED"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a real Runpod pod and verify server-side keep-alive stops it.")
    parser.add_argument("--gpu-type-id", default="NVIDIA L40S")
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY"], default="SECURE")
    parser.add_argument("--timeout-seconds", type=int, default=10)
    parser.add_argument("--shutdown-timeout-seconds", type=int, default=180)
    parser.add_argument("--output", default="user/runpod-agentic/keepalive-timeout-result.json")
    args = parser.parse_args()

    deployment = build_deployment(args.gpu_type_id, args.gpu_count, args.cloud_type, args.timeout_seconds)
    plan = Planner().build(
        deployment,
        mode="plan",
        prompt="Runpod keep-alive timeout smoke test.",
        workflow_graph={"runpod_keepalive_timeout": True, "timeout_seconds": args.timeout_seconds},
    )
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    stop_after = agent.pod_input.get("stopAfter")
    if not stop_after:
        raise AssertionError(f"Planner did not attach stopAfter to the Runpod pod input: {agent.pod_input}")
    stop_after_at = datetime.fromisoformat(stop_after)
    if stop_after_at.tzinfo is None:
        stop_after_at = stop_after_at.replace(tzinfo=UTC)
    stop_after_delta = (stop_after_at - datetime.now(UTC)).total_seconds()
    if stop_after_delta < 1 or stop_after_delta > args.timeout_seconds + 30:
        raise AssertionError(f"Planner stopAfter was not near the requested timeout: {stop_after}")

    client = RunpodClient()
    pod_id = ""
    observations: list[dict[str, object]] = []
    try:
        pod = client.create_or_deploy_pod(agent.pod_input)
        pod_id = pod["id"]
        scheduled = schedule_runpod_lifecycle(pod_id, "stop", args.timeout_seconds)
        observations.append(observation(pod))
        stopped = wait_for_stopped(client, pod_id, args.shutdown_timeout_seconds, observations)
        result = {
            "pod_id": pod_id,
            "gpu_type_id": args.gpu_type_id,
            "cloud_type": args.cloud_type,
            "timeout_seconds": args.timeout_seconds,
            "stop_after": stop_after,
            "scheduled_keep_alive": scheduled,
            "stopped": stopped,
            "observations": observations,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if pod_id:
            terminate_with_retry(client, pod_id)


def build_deployment(gpu_type_id: str, gpu_count: int, cloud_type: str, timeout_seconds: int):
    keep_alive = KeepAliveNode().build("time", "stop", timeout_seconds, "seconds", 0, 0.0, 0, "server_side")[0]
    agent = AgentNode().build("Pi", "keepalive-timeout", "manual", "/workspace", node_id="runpod-keepalive-agent")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive, node_id="runpod-keepalive-deploy")[0]
    return with_terminal_options(
        deployment,
        gpu_type_id=gpu_type_id,
        gpu_count=gpu_count,
        cloud_type=cloud_type,
        container_disk_gb=20,
        volume_gb=0,
        expose_public_ip=True,
        reuse_policy="always_create",
    )


def wait_for_stopped(client: RunpodClient, pod_id: str, timeout_seconds: int, observations: list[dict[str, object]]) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            pod = client.get_pod(pod_id)
        except RunpodClientError as exc:
            observations.append({"id": pod_id, "error": str(exc), "observed_at": datetime.now(UTC).isoformat()})
            time.sleep(3)
            continue
        item = observation(pod)
        observations.append(item)
        if item["desired_status"] in STOPPED_STATUSES:
            return item
        time.sleep(3)
    raise AssertionError(f"Timed out waiting for Runpod keep-alive to stop pod {pod_id}: {observations[-5:]}")


def terminate_with_retry(client: RunpodClient, pod_id: str) -> None:
    for attempt in range(1, 6):
        try:
            client.terminate_pod(pod_id)
            return
        except RunpodClientError:
            if attempt == 5:
                raise
            time.sleep(3)


def observation(pod: dict) -> dict[str, object]:
    runtime = pod.get("runtime") or {}
    return {
        "id": pod.get("id"),
        "desired_status": pod.get("desiredStatus"),
        "runtime_present": bool(runtime),
        "uptime_seconds": runtime.get("uptimeInSeconds"),
        "observed_at": datetime.now(UTC).isoformat(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
