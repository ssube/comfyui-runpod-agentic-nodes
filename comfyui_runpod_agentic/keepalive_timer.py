from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

from .runpod_client import RunpodClient, RunpodClientError


def schedule_runpod_lifecycle(pod_id: str, action: str, delay_seconds: int) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "comfyui_runpod_agentic.keepalive_timer",
        "--pod-id",
        pod_id,
        "--action",
        action,
        "--delay-seconds",
        str(int(delay_seconds)),
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"pod_id": pod_id, "action": action, "delay_seconds": int(delay_seconds), "pid": process.pid, "command": command}


def apply_runpod_lifecycle(pod_id: str, action: str, delay_seconds: int, *, attempts: int = 5) -> dict[str, Any]:
    time.sleep(max(0, int(delay_seconds)))
    client = RunpodClient()
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            if action == "terminate":
                client.terminate_pod(pod_id)
                return {"pod_id": pod_id, "action": action, "attempt": attempt}
            result = client.stop_pod(pod_id)
            return {"pod_id": pod_id, "action": action, "attempt": attempt, "result": result}
        except RunpodClientError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(3)
    raise RuntimeError(f"Runpod keep-alive {action} failed for pod {pod_id}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a delayed Runpod keep-alive lifecycle action.")
    parser.add_argument("--pod-id", required=True)
    parser.add_argument("--action", choices=["stop", "terminate"], required=True)
    parser.add_argument("--delay-seconds", type=int, required=True)
    args = parser.parse_args()

    result = apply_runpod_lifecycle(args.pod_id, args.action, args.delay_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
