from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

FIT_WORKFLOW_SCRIPT = """
async ({ workflow }) => {
  const data = JSON.parse(workflow);
  const app = window.app;
  if (!app || !app.graph) {
    throw new Error("ComfyUI app graph is not available");
  }

  if (typeof app.loadGraphData === "function") {
    await app.loadGraphData(data);
  } else if (typeof app.graph.configure === "function") {
    app.graph.configure(data);
  } else {
    throw new Error("No supported ComfyUI workflow loader was found");
  }

  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

  const graph = app.graph;
  const canvas = app.canvas;
  const canvasElement = canvas?.canvas || document.querySelector("canvas");
  if (!graph?._nodes?.length || !canvas || !canvasElement) {
    throw new Error("Workflow graph or canvas was not available after loading");
  }

  const bounds = graph._nodes.reduce(
    (acc, node) => {
      const pos = node.pos || [0, 0];
      const size = node.size || [240, 120];
      acc.minX = Math.min(acc.minX, pos[0]);
      acc.minY = Math.min(acc.minY, pos[1]);
      acc.maxX = Math.max(acc.maxX, pos[0] + size[0]);
      acc.maxY = Math.max(acc.maxY, pos[1] + size[1]);
      return acc;
    },
    { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity },
  );

  const viewWidth = window.innerWidth || canvasElement.clientWidth;
  const viewHeight = window.innerHeight || canvasElement.clientHeight;
  const padding = 160;
  const graphWidth = Math.max(1, bounds.maxX - bounds.minX);
  const graphHeight = Math.max(1, bounds.maxY - bounds.minY);
  const scale = Math.min(1.0, (viewWidth - padding) / graphWidth, (viewHeight - padding) / graphHeight);

  canvas.ds.scale = Math.max(0.1, scale);
  canvas.ds.offset = [
    (viewWidth / canvas.ds.scale - graphWidth) / 2 - bounds.minX,
    (viewHeight / canvas.ds.scale - graphHeight) / 2 - bounds.minY,
  ];

  if (typeof graph.setDirtyCanvas === "function") {
    graph.setDirtyCanvas(true, true);
  }
  if (typeof canvas.setDirty === "function") {
    canvas.setDirty(true, true);
  }

  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return { nodes: graph._nodes.length, scale: canvas.ds.scale, offset: canvas.ds.offset };
}
"""

ENSURE_GRAPH_MODE_SCRIPT = """
async () => {
  const button = document.querySelector('[aria-label="Enter node graph"]');
  if (button) {
    button.click();
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return {
    mode: document.querySelector('[aria-label="Enter app mode"]') ? "graph" : "unknown",
  };
}
"""

ENSURE_APP_MODE_SCRIPT = """
async () => {
  if (document.querySelector('[aria-label="Enter node graph"]')) {
    return {
      mode: "app",
      title: document.title,
      bodyText: document.body.innerText.slice(0, 2000),
    };
  }
  const button =
    document.querySelector('[aria-label="Enter app mode"]') ||
    [...document.querySelectorAll('button, [role="button"]')].find((element) => {
      const label = `${element.getAttribute('aria-label') || ''} ${element.getAttribute('title') || ''} ${element.textContent || ''}`.toLowerCase();
      return label.includes('app mode') || label.includes('linear mode');
    });
  if (!button) {
    throw new Error("Could not find the App mode button");
  }
  button.click();
  await new Promise((resolve) => setTimeout(resolve, 1000));
  return {
    mode: "app",
    title: document.title,
    bodyText: document.body.innerText.slice(0, 2000),
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch ComfyUI, load UI workflow JSON files, and screenshot each workflow in graph mode and app mode.")
    parser.add_argument("--comfy-dir", default=os.environ.get("COMFYUI_E2E_DIR", "/tmp/comfyui-runpod-e2e"))
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=int(os.environ.get("COMFYUI_SCREENSHOT_PORT", "0")))
    parser.add_argument("--workflow", action="append", default=[], help="Workflow JSON file or glob. Defaults to examples/workflows/ui_*.json.")
    parser.add_argument("--output-dir", default="artifacts/workflow-screenshots")
    parser.add_argument("--install-deps", action="store_true", default=os.environ.get("COMFYUI_E2E_INSTALL_DEPS") == "1")
    parser.add_argument("--skip-clone", action="store_true", default=os.environ.get("COMFYUI_E2E_SKIP_CLONE") == "1")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--browser-executable", default=os.environ.get("COMFYUI_SCREENSHOT_BROWSER", ""))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--viewport-width", type=int, default=2560)
    parser.add_argument("--viewport-height", type=int, default=1440)
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    comfy_dir = Path(args.comfy_dir).resolve()
    workflows = resolve_workflows(repo_dir, args.workflow)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    ensure_comfyui(comfy_dir, skip_clone=args.skip_clone)
    if args.install_deps:
        run([args.python, "-m", "pip", "install", "-r", str(comfy_dir / "requirements.txt")], cwd=repo_dir)

    with tempfile.TemporaryDirectory(prefix="runpod-comfy-screens-") as tmp:
        base_dir = Path(tmp) / "base"
        user_dir = Path(tmp) / "user"
        custom_nodes = base_dir / "custom_nodes"
        custom_nodes.mkdir(parents=True)
        user_dir.mkdir(parents=True)
        (custom_nodes / "comfyui-runpod-agentic").symlink_to(repo_dir, target_is_directory=True)

        port = args.port or default_port()
        proc = start_comfy(args.python, comfy_dir, base_dir, user_dir, port)
        output: list[str] = []
        try:
            wait_for_server(port, proc, output, timeout=args.timeout)
            screenshot_count = capture_screenshots(
                f"http://127.0.0.1:{port}",
                workflows,
                output_dir,
                headed=args.headed,
                browser_executable=args.browser_executable or None,
                timeout=args.timeout,
                viewport=(args.viewport_width, args.viewport_height),
            )
        finally:
            stop_process(proc)

    print(f"Wrote {screenshot_count} workflow screenshots to {output_dir}")
    return 0


def resolve_workflows(repo_dir: Path, patterns: list[str]) -> list[Path]:
    selected = patterns or ["examples/workflows/ui_*.json"]
    workflows: list[Path] = []
    for pattern in selected:
        path = Path(pattern)
        matches = sorted((repo_dir if not path.is_absolute() else Path("/")).glob(pattern if not path.is_absolute() else str(path)[1:]))
        if matches:
            workflows.extend(match.resolve() for match in matches)
        elif path.exists():
            workflows.append(path.resolve())
        else:
            raise SystemExit(f"Workflow path or glob did not match: {pattern}")
    unique = sorted(dict.fromkeys(workflows))
    if not unique:
        raise SystemExit("No workflow files were selected")
    for workflow in unique:
        json.loads(workflow.read_text())
    return unique


def capture_screenshots(
    server: str,
    workflows: list[Path],
    output_dir: Path,
    *,
    headed: bool,
    browser_executable: str | None,
    timeout: int,
    viewport: tuple[int, int],
) -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright is required. Run `python -m pip install -e .[dev]` and `python -m playwright install chromium`.") from exc

    with sync_playwright() as playwright:
        try:
            launch_options = {"headless": not headed, "args": ["--no-sandbox"]}
            executable = browser_executable or system_browser()
            if executable:
                launch_options["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightError as exc:
            raise SystemExit(
                "Chromium is not available. Run `python -m playwright install chromium` "
                "or set COMFYUI_SCREENSHOT_BROWSER=/path/to/chrome."
            ) from exc
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        screenshot_count = 0

        for workflow in workflows:
            workflow_text = workflow.read_text()
            page.goto(server, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_function("() => window.app && window.app.graph", timeout=timeout * 1000)
            page.set_viewport_size(workflow_viewport(json.loads(workflow_text), minimum=viewport))
            dismiss_overlays(page)
            page.evaluate(ENSURE_GRAPH_MODE_SCRIPT)
            result = page.evaluate(FIT_WORKFLOW_SCRIPT, {"workflow": workflow_text})
            page.evaluate(ENSURE_GRAPH_MODE_SCRIPT)
            dismiss_overlays(page)
            screenshot_path = output_dir / f"{workflow.stem}.png"
            page.screenshot(path=str(screenshot_path), timeout=timeout * 1000)
            screenshot_count += 1
            print(
                f"{workflow.name} graph: {result['nodes']} nodes at scale {result['scale']:.3f} "
                f"in {page.viewport_size['width']}x{page.viewport_size['height']} -> {screenshot_path}"
            )
            app_result = page.evaluate(ENSURE_APP_MODE_SCRIPT)
            dismiss_overlays(page)
            app_screenshot_path = output_dir / f"{workflow.stem}_app.png"
            page.screenshot(path=str(app_screenshot_path), timeout=timeout * 1000)
            screenshot_count += 1
            print(
                f"{workflow.name} app: {app_result['title'] or 'App mode'} "
                f"in {page.viewport_size['width']}x{page.viewport_size['height']} -> {app_screenshot_path}"
            )
        browser.close()
        return screenshot_count


def dismiss_overlays(page) -> None:
    page.keyboard.press("Escape")
    page.evaluate(
        """
        () => {
          for (const element of document.querySelectorAll('button, [role="button"]')) {
            if (!element.closest('[role="dialog"], .p-dialog, .modal, .comfy-modal')) {
              continue;
            }
            const label = `${element.getAttribute('aria-label') || ''} ${element.getAttribute('title') || ''} ${element.textContent || ''}`.toLowerCase();
            if (label.includes('close') || label.includes('dismiss') || label.trim() === 'x' || label.trim() === '×') {
              element.click();
            }
          }
        }
        """
    )
    page.wait_for_timeout(300)


def system_browser() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def workflow_viewport(workflow: dict, *, minimum: tuple[int, int]) -> dict[str, int]:
    bounds = workflow_bounds(workflow)
    padding = 360
    width = max(minimum[0], int(bounds["width"] + padding))
    height = max(minimum[1], int(bounds["height"] + padding))
    return {"width": min(width, 4096), "height": min(height, 2400)}


def workflow_bounds(workflow: dict) -> dict[str, float]:
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    for node in workflow.get("nodes", []):
        pos = node.get("pos") or [0, 0]
        size = node.get("size") or [240, 120]
        min_x = min(min_x, float(pos[0]))
        min_y = min(min_y, float(pos[1]))
        max_x = max(max_x, float(pos[0]) + float(size[0]))
        max_y = max(max_y, float(pos[1]) + float(size[1]))
    if min_x == float("inf"):
        raise ValueError("Workflow has no nodes")
    return {"width": max_x - min_x, "height": max_y - min_y}


def ensure_comfyui(comfy_dir: Path, *, skip_clone: bool) -> None:
    if (comfy_dir / "main.py").exists():
        return
    if skip_clone:
        raise SystemExit(f"ComfyUI checkout missing: {comfy_dir}")
    comfy_dir.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--depth", "1", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)], cwd=comfy_dir.parent)


def start_comfy(python: str, comfy_dir: Path, base_dir: Path, user_dir: Path, port: int) -> subprocess.Popen[str]:
    cmd = [
        python,
        str(comfy_dir / "main.py"),
        "--cpu",
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--base-directory",
        str(base_dir),
        "--user-directory",
        str(user_dir),
        "--disable-api-nodes",
        "--whitelist-custom-nodes",
        "comfyui-runpod-agentic",
        "--log-stdout",
    ]
    return subprocess.Popen(cmd, cwd=comfy_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for_server(port: int, proc: subprocess.Popen[str], output: list[str], *, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            if proc.stdout:
                output.append(proc.stdout.read())
            raise AssertionError(f"ComfyUI server exited early with {proc.returncode}\n{''.join(output)}")
        if proc.stdout:
            drain_stdout(proc, output)
        try:
            fetch_json(f"http://127.0.0.1:{port}/object_info")
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(1)
    raise AssertionError(f"ComfyUI server did not become ready\n{''.join(output)}")


def drain_stdout(proc: subprocess.Popen[str], output: list[str]) -> None:
    import select

    assert proc.stdout is not None
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], 0)
        if not ready:
            return
        line = proc.stdout.readline()
        if not line:
            return
        output.append(line)


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def default_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except OSError:
        return 18100 + (os.getpid() % 20000)


def stop_process(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def run(cmd: list[str], cwd: Path) -> None:
    if shutil.which(cmd[0]) is None and not Path(cmd[0]).exists():
        raise SystemExit(f"Command not found: {cmd[0]}")
    subprocess.run(cmd, cwd=cwd, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
