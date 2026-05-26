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
    MCPServerNode,
    RunLocalContainersNode,
    SSHCommandNode,
)

MARKER = "CRAG_MCP_SECRET_FROM_FILESYSTEM_SERVER"
PROJECT_NAME = "crag-local-mcp-ollama-cloud"
RESPONSE_PATH = "/workspace/.runpod_agentic/response.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live local Pi + Ollama Cloud e2e that uses a stdio MCP filesystem server.")
    parser.add_argument("--engine", choices=["containerd"], default="containerd")
    parser.add_argument("--project-name", default=PROJECT_NAME)
    parser.add_argument("--output-path", default="artifacts/local-runtime/mcp-ollama-cloud-compose.yaml")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--sudo-runtime", action="store_true", default=os.environ.get("CRAG_LOCAL_RUNTIME_SUDO") == "1")
    args = parser.parse_args()

    if not shutil.which("nerdctl"):
        raise SystemExit("nerdctl is required for the live MCP Ollama Cloud e2e.")
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
                "Use the crag_mcp_read_file tool with server=filesystem and path=/workspace/e2e/mcp-secret.txt. "
                "Reply with the exact token CRAG_MCP_LIVE_OK and the file contents."
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
            raise AssertionError(f"MCP Ollama Cloud apply failed:\n{result_text}\n{errors}")
        if "[crag-agent] complete status=0" not in response:
            raise AssertionError(f"Pi did not complete successfully:\nresponse:\n{response}\nerrors:\n{errors}")
        if "CRAG_MCP_LIVE_OK" not in response or MARKER not in response:
            raise AssertionError(f"Pi response did not prove MCP tool use:\nresponse:\n{response}\nerrors:\n{errors}")

        agent_id = agent_container_id(args.project_name)
        try:
            tool_result = file_text(agent_id, "/workspace/e2e/mcp-tool-result.json")
        except AssertionError as exc:
            raise AssertionError(f"MCP bridge proof file was not created:\nresponse:\n{response}\nerrors:\n{errors}") from exc
        tool_payload = json.loads(tool_result)
        if tool_payload.get("tool") != "read_file" or MARKER not in json.dumps(tool_payload):
            raise AssertionError(f"MCP bridge did not record a read_file result:\n{tool_result}")
        mcp_config = file_text(agent_id, "/workspace/.runpod_agentic/mcp_servers.json")
        if "filesystem" not in mcp_config:
            raise AssertionError(f"MCP config was not written into the agent runtime:\n{mcp_config}")

        print(
            json.dumps(
                {
                    "compose_path": saved_path,
                    "compose_yaml_bytes": len(compose_yaml.encode()),
                    "response_excerpt": response[:2000],
                    "tool_result": tool_payload,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        node.apply(
            deployment,
            prompt="Terminate MCP Ollama Cloud e2e.",
            project_name=args.project_name,
            output_path=args.output_path,
            action="terminate",
            use_sudo=args.sudo_runtime,
            timeout_seconds=300,
            response_timeout_seconds=0,
        )


def build_deployment():
    llm = LLMApiNode().build("Ollama Cloud", "deepseek-v4-flash", "OLLAMA_API_KEY", "")[0]
    mcp = MCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace/e2e", "", "{}", "")[0]
    agent = AgentNode().build(
        "Pi",
        "deepseek-v4-flash",
        "auto_start",
        "/workspace",
        system_prompt=(
            "You are verifying CRAG MCP support. You must call the crag_mcp_read_file tool before answering. "
            "If the tool returns file contents, include them verbatim."
        ),
        llm=llm,
        mcp_servers=mcp,
    )[0]
    setup = SSHCommandNode().build(mcp_setup_command(), "before_start", "fail", retry_count=1)[0]
    return DeployNode().build(agent, commands=setup)[0]


def mcp_setup_command() -> str:
    return r"""set -e
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"
mkdir -p /workspace/e2e /workspace/.pi/extensions/crag-mcp
printf '%s\n' 'CRAG_MCP_SECRET_FROM_FILESYSTEM_SERVER' > /workspace/e2e/mcp-secret.txt
cat > /workspace/.pi/extensions/crag-mcp/package.json <<'PACKAGE_JSON'
{
  "type": "module",
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.21.0",
    "typebox": "^1.0.58"
  }
}
PACKAGE_JSON
npm install --prefix /workspace/.pi/extensions/crag-mcp --omit=dev
cat > /workspace/.pi/extensions/crag-mcp/index.ts <<'EXTENSION_TS'
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type ServerConfig = {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  transport?: string;
};

function loadConfig(): Record<string, ServerConfig> {
  const raw = process.env.MCP_SERVERS_JSON || readFileSync(process.env.MCP_SERVERS_FILE || "/workspace/.runpod_agentic/mcp_servers.json", "utf8");
  const parsed = JSON.parse(raw);
  return parsed.mcpServers || {};
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "crag_mcp_read_file",
    label: "CRAG MCP read file",
    description: "Read a file by connecting to a CRAG-configured stdio MCP server and calling its read_file tool. Use this when asked to verify MCP access.",
    parameters: Type.Object({
      server: Type.String({ description: "MCP server name from mcp_servers.json, usually filesystem" }),
      path: Type.String({ description: "Absolute file path to read through the MCP server" }),
    }),
    async execute(_toolCallId, params) {
      const servers = loadConfig();
      const server = servers[params.server];
      if (!server || server.transport !== "stdio" || !server.command) {
        throw new Error(`Unsupported or missing MCP server: ${params.server}`);
      }
      const transport = new StdioClientTransport({
        command: server.command,
        args: server.args || [],
        env: { ...process.env, ...(server.env || {}) },
      });
      const client = new Client({ name: "crag-mcp-e2e", version: "1.0.0" });
      try {
        await client.connect(transport);
        const tools = await client.listTools();
        const result = await client.callTool({ name: "read_file", arguments: { path: params.path } });
        const payload = { server: params.server, tool: "read_file", tools, result };
        mkdirSync(dirname("/workspace/e2e/mcp-tool-result.json"), { recursive: true });
        writeFileSync("/workspace/e2e/mcp-tool-result.json", JSON.stringify(payload, null, 2));
        return {
          content: [{ type: "text", text: JSON.stringify(payload) }],
          details: payload,
        };
      } finally {
        await client.close();
      }
    },
  });
}
EXTENSION_TS
mkdir -p /workspace/.runpod_agentic/launcher.d/pre.d
cat > /workspace/e2e/run-pi-mcp.sh <<'RUN_PI_MCP'
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
  --extension /workspace/.pi/extensions/crag-mcp/index.ts
  --no-builtin-tools
  --tools crag_mcp_read_file
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
  echo "prompt_file: ${AGENT_PROMPT_FILE:-}"
  echo "system_prompt_file: ${AGENT_SYSTEM_PROMPT_FILE:-}"
  echo "mcp_servers_file: ${MCP_SERVERS_FILE:-}"
  echo "extension: /workspace/.pi/extensions/crag-mcp/index.ts"
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
RUN_PI_MCP
chmod +x /workspace/e2e/run-pi-mcp.sh
cat > /workspace/.runpod_agentic/launcher.d/pre.d/50-crag-mcp-extension.sh <<'HOOK_SH'
export CRAG_AGENT_LAUNCH_COMMAND=/workspace/e2e/run-pi-mcp.sh
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
