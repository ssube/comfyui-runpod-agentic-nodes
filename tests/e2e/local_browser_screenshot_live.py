from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from comfyui_runpod_agentic.nodes import AgentNode, BrowserNode, DeployNode, LLMApiNode, RunLocalContainersNode, SSHCommandNode

PROJECT_NAME = "crag-local-browser-screenshot"
RESPONSE_PATH = "/workspace/.runpod_agentic/response.txt"
IMAGE_PATH = "/workspace/e2e/browser-screenshot.png"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live local browser screenshots through a CRAG skill, using Playwright first and then Neko.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-prefix", default=PROJECT_NAME)
    parser.add_argument("--output-dir", default="artifacts/local-runtime")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the live browser screenshot e2e.")
    if not containerd_runtime_ready(args.sudo_runtime):
        hint = "start rootless containerd or pass --sudo-runtime for a system containerd socket"
        raise SystemExit(f"containerd local runtime is not running; {hint} before running local e2e.")
    if not has_ollama_key():
        raise SystemExit("OLLAMA_API_KEY is required in .env.d/ollama.env or the process environment.")
    if args.sudo_runtime:
        os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"

    results = []
    for browser, placement, marker in (("Playwright", "own_pod", "CRAG_BROWSER_PLAYWRIGHT_OK"), ("Neko", "own_pod", "CRAG_BROWSER_NEKO_OK")):
        results.append(run_browser_case(args, browser=browser, placement=placement, marker=marker))
    print(json.dumps({"results": results}, indent=2, sort_keys=True))
    return 0


def run_browser_case(args: argparse.Namespace, *, browser: str, placement: str, marker: str) -> dict[str, object]:
    project = f"{args.project_prefix}-{browser.lower()}"
    output_path = f"{args.output_dir}/{project}-compose.yaml"
    deployment = build_deployment(browser, placement)
    node = RunLocalContainersNode()
    try:
        result_text, response, errors, compose_yaml, saved_path, image = node.apply(
            deployment,
            engine=args.engine,
            prompt=(
                "Use the crag_browser_screenshot tool from the crag-browser-screenshot skill with "
                f"url=https://example.com and output_path={IMAGE_PATH}. Reply with {marker}, "
                "the tool strategy, and the screenshot path."
            ),
            project_name=project,
            output_path=output_path,
            action="apply_and_wait",
            use_sudo=args.sudo_runtime,
            timeout_seconds=args.timeout_seconds,
            response_path=RESPONSE_PATH,
            response_image_path=IMAGE_PATH,
            response_timeout_seconds=args.timeout_seconds,
            reuse_policy="always_create",
        )
        result = json.loads(result_text)
        if result["returncode"] != 0:
            raise AssertionError(f"{browser} browser apply failed:\n{result_text}\n{errors}")
        if "[crag-agent] complete status=0" not in response or marker not in response:
            raise AssertionError(f"{browser} Pi response did not prove skill use:\nresponse:\n{response}\nerrors:\n{errors}")
        if tuple(image.shape)[1] <= 1 or tuple(image.shape)[2] <= 1:
            raise AssertionError(f"{browser} run node did not emit a real image tensor: shape={tuple(image.shape)}")

        agent_id = agent_container_id(project)
        report = json.loads(file_text(agent_id, "/workspace/e2e/browser-screenshot-report.json"))
        if report.get("url") != "https://example.com":
            raise AssertionError(f"{browser} skill report did not target example.com:\n{json.dumps(report, indent=2)}")
        if report.get("browser_kind") != browser.lower():
            raise AssertionError(f"{browser} skill report used the wrong browser kind:\n{json.dumps(report, indent=2)}")
        if int(report.get("bytes") or 0) <= 0:
            raise AssertionError(f"{browser} skill report did not record a screenshot:\n{json.dumps(report, indent=2)}")

        return {
            "browser": browser,
            "compose_path": saved_path,
            "compose_yaml_bytes": len(compose_yaml.encode()),
            "image_shape": list(image.shape),
            "report": report,
            "response_excerpt": response[:1200],
        }
    finally:
        node.apply(
            deployment,
            prompt=f"Terminate {browser} browser screenshot e2e.",
            project_name=project,
            output_path=output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=300,
            response_timeout_seconds=0,
        )


def build_deployment(browser: str, placement: str):
    llm = LLMApiNode().build("Ollama Cloud", "deepseek-v4-flash", "OLLAMA_API_KEY", "")[0]
    browser_spec = BrowserNode().build(browser, placement, "chromium")[0]
    agent = AgentNode().build(
        "Pi",
        "deepseek-v4-flash",
        "auto_start",
        "/workspace",
        system_prompt=(
            "You are verifying CRAG browser skills. You must call the crag_browser_screenshot tool before answering. "
            "Include the exact success token from the user only after the tool succeeds."
        ),
        browser=browser_spec,
        llm=llm,
    )[0]
    setup = SSHCommandNode().build(browser_setup_command(), "before_start", "fail", retry_count=1)[0]
    return DeployNode().build(agent, commands=setup)[0]


def browser_setup_command() -> str:
    return r"""set -e
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv
python3 -m pip install --break-system-packages playwright requests
python3 -m playwright install --with-deps chromium
mkdir -p /workspace/e2e /workspace/.runpod_agentic/skills/crag-browser-screenshot /workspace/.pi/extensions/crag-browser-screenshot
cat > /workspace/.runpod_agentic/skills/crag-browser-screenshot/SKILL.md <<'SKILL_MD'
---
name: crag-browser-screenshot
description: Take a screenshot of a public URL with the CRAG browser integration and write a report.
---

Use `python3 /workspace/.runpod_agentic/skills/crag-browser-screenshot/take_screenshot.py --url <url> --output <path>` to take a screenshot.
The script chooses Playwright when `BROWSER_KIND=playwright` and Neko when `BROWSER_KIND=neko`.
SKILL_MD
cat > /workspace/.runpod_agentic/skills/crag-browser-screenshot/take_screenshot.py <<'PY'
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", default="/workspace/e2e/browser-screenshot-report.json")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    browser_kind = os.environ.get("BROWSER_KIND", "playwright")
    if browser_kind == "neko":
        strategy = screenshot_with_neko(args.url, output)
    else:
        strategy = screenshot_with_playwright(args.url, output)
    report = {
        "browser_kind": browser_kind,
        "url": args.url,
        "output": str(output),
        "bytes": output.stat().st_size,
        "strategy": strategy,
    }
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def screenshot_with_playwright(url: str, output: Path) -> str:
    with sync_playwright() as playwright:
        endpoint = os.environ.get("PLAYWRIGHT_WS_ENDPOINT", "").strip()
        if endpoint:
            if endpoint.startswith("http://"):
                endpoint = "ws://" + endpoint.removeprefix("http://")
            elif endpoint.startswith("https://"):
                endpoint = "wss://" + endpoint.removeprefix("https://")
            browser = playwright.chromium.connect(endpoint)
            strategy = "playwright-remote"
        else:
            browser = playwright.chromium.launch(headless=True)
            strategy = "playwright-local"
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.screenshot(path=str(output), full_page=True)
        browser.close()
    return strategy


def screenshot_with_neko(url: str, output: Path) -> str:
    neko_url = os.environ["NEKO_URL"].rstrip("/")
    candidates = (
        f"{neko_url}/api/room/screen/shot.jpg",
        f"{neko_url}/api/screenshot.jpg",
        f"{neko_url}/screenshot.jpg?pwd=admin",
    )
    for candidate in candidates:
        response = requests.get(candidate, timeout=30)
        if response.ok and response.content:
            output.write_bytes(response.content)
            return f"neko-screenshot-api:{candidate}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(f"{neko_url}/?usr=neko&pwd=neko", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(output), full_page=True)
        browser.close()
    return "neko-web-ui-fallback"


if __name__ == "__main__":
    raise SystemExit(main())
PY
cat > /workspace/.pi/extensions/crag-browser-screenshot/package.json <<'PACKAGE_JSON'
{
  "type": "module",
  "dependencies": {
    "typebox": "^1.0.58"
  }
}
PACKAGE_JSON
npm install --prefix /workspace/.pi/extensions/crag-browser-screenshot --omit=dev
cat > /workspace/.pi/extensions/crag-browser-screenshot/index.ts <<'EXTENSION_TS'
import { execFileSync } from "node:child_process";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "crag_browser_screenshot",
    label: "CRAG browser screenshot",
    description: "Use the CRAG browser skill to capture a screenshot of a public URL and write a report. Use this when asked to verify browser access.",
    parameters: Type.Object({
      url: Type.String({ description: "Public URL to capture" }),
      output_path: Type.String({ description: "Container path for the screenshot image" }),
    }),
    async execute(_toolCallId, params) {
      const stdout = execFileSync("python3", [
        "/workspace/.runpod_agentic/skills/crag-browser-screenshot/take_screenshot.py",
        "--url",
        params.url,
        "--output",
        params.output_path,
      ], { encoding: "utf8" });
      return {
        content: [{ type: "text", text: stdout }],
        details: JSON.parse(stdout),
      };
    },
  });
}
EXTENSION_TS
mkdir -p /workspace/.runpod_agentic/launcher.d/pre.d
cat > /workspace/e2e/run-pi-browser.sh <<'RUN_PI_BROWSER'
#!/usr/bin/env bash
set -euo pipefail

response_file="${AGENT_RESPONSE_FILE:-$CRAG_RUNTIME_DIR/response.txt}"
errors_file="${AGENT_ERRORS_FILE:-$CRAG_RUNTIME_DIR/errors.txt}"
mkdir -p "$(dirname "$response_file")" "$(dirname "$errors_file")"
prompt=""
if [ -f "$AGENT_PROMPT_FILE" ]; then
  prompt="$(cat "$AGENT_PROMPT_FILE")"
fi
args=(
  --extension /workspace/.pi/extensions/crag-browser-screenshot/index.ts
  --no-builtin-tools
  --tools crag_browser_screenshot
)
if [ "${LLM_PROVIDER:-}" = "ollama_cloud" ]; then
  args+=(--provider ollama-cloud)
fi
if [ -n "${AGENT_MODEL:-}" ]; then
  args+=(--model "$AGENT_MODEL")
fi
if [ -s "$AGENT_SYSTEM_PROMPT_FILE" ]; then
  args+=(--system-prompt "$(cat "$AGENT_SYSTEM_PROMPT_FILE")")
fi
status=0
set +e
{
  echo "harness: ${AGENT_HARNESS:-}"
  echo "model: ${AGENT_MODEL:-}"
  echo "browser_kind: ${BROWSER_KIND:-}"
  echo "playwright_mode: ${PLAYWRIGHT_MODE:-}"
  echo "neko_url: ${NEKO_URL:-}"
  echo
  pi "${args[@]}" -p "$prompt"
  status=$?
  echo
  echo "[crag-agent] complete status=$status"
} > "$response_file" 2> "$errors_file"
set -e
cat "$response_file"
if [ -s "$errors_file" ]; then
  cat "$errors_file" >&2
fi
exit "$status"
RUN_PI_BROWSER
chmod +x /workspace/e2e/run-pi-browser.sh
cat > /workspace/.runpod_agentic/launcher.d/pre.d/50-crag-browser-extension.sh <<'HOOK_SH'
export CRAG_AGENT_LAUNCH_COMMAND=/workspace/e2e/run-pi-browser.sh
HOOK_SH
"""


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
