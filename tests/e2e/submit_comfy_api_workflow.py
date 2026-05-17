from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit a ComfyUI API JSON workflow and wait for prompt completion.")
    parser.add_argument("--server", default="http://127.0.0.1:8199")
    parser.add_argument("--workflow", default="examples/workflows/api_plan_smoke.json")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    prompt = json.loads(Path(args.workflow).read_text())
    client_id = uuid.uuid4().hex
    response = post_json(f"{args.server}/prompt", {"prompt": prompt, "client_id": client_id})
    prompt_id = response["prompt_id"]
    history = wait_history(args.server, prompt_id, args.timeout)
    print(json.dumps({"prompt_id": prompt_id, "history": history}, indent=2, sort_keys=True))
    return 0


def wait_history(server: str, prompt_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            history = fetch_json(f"{server}/history/{prompt_id}")
        except urllib.error.URLError:
            history = {}
        if history:
            return history
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for prompt history: {prompt_id}")


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
