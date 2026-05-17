from __future__ import annotations

import argparse
import json

from .runpod_client import RunpodClient


def cleanup_managed_pods(action: str = "terminate", prefix: str = "crag-") -> list[dict]:
    client = RunpodClient()
    affected = []
    for pod in client.list_pods():
        if not str(pod.get("name", "")).startswith(prefix):
            continue
        pod_id = pod["id"]
        if action == "terminate":
            client.terminate_pod(pod_id)
        else:
            client.stop_pod(pod_id)
        affected.append({"id": pod_id, "name": pod.get("name"), "action": action})
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop or terminate Runpod pods managed by this project.")
    parser.add_argument("--action", choices=["stop", "terminate"], default="terminate")
    parser.add_argument("--prefix", default="crag-")
    args = parser.parse_args()
    print(json.dumps({"affected": cleanup_managed_pods(args.action, args.prefix)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
