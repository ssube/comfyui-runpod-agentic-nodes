from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import read_env_file
from .planner import DeploymentPlan, ResourcePlan
from .runner import launch_command_for_plan, launcher_runtime_files, pi_runtime_files, startup_script_for_plan

DEFAULT_IMAGES = {
    "agent": "ubuntu:24.04",
    "browser:neko": "ghcr.io/m1k1o/neko/chromium:latest",
    "browser:playwright": "mcr.microsoft.com/playwright:v1.56.1-noble",
    "llm:ollama": "ollama/ollama:0.12.10",
    "llm:vllm": "vllm/vllm-openai:latest",
    "sql:postgres": "postgres:17.7",
    "sql:mysql": "mysql:8.4",
    "vector:qdrant": "qdrant/qdrant:v1.16.2",
    "vector:chroma": "chromadb/chroma:latest",
}

LOCAL_RUNTIME_ACTIONS = ("save_only", "config", "pull", "apply", "apply_and_wait", "stop", "terminate", "destroy", "up", "down")


@dataclass(frozen=True)
class LocalApplyResult:
    engine: str
    action: str
    compose_path: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_text(self) -> str:
        return json.dumps(
            {
                "engine": self.engine,
                "action": self.action,
                "compose_path": self.compose_path,
                "command": self.command,
                "returncode": self.returncode,
                "stdout": self.stdout,
                "stderr": self.stderr,
            },
            indent=2,
            sort_keys=True,
        )


@dataclass(frozen=True)
class LocalRuntimeReadResult:
    engine: str
    project_name: str
    role: str
    path: str
    container_id: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_text(self) -> str:
        return json.dumps(
            {
                "engine": self.engine,
                "project_name": self.project_name,
                "role": self.role,
                "path": self.path,
                "container_id": self.container_id,
                "command": self.command,
                "returncode": self.returncode,
                "stdout": self.stdout,
                "stderr": self.stderr,
            },
            indent=2,
            sort_keys=True,
        )


def compose_yaml_for_plan(plan: DeploymentPlan, *, project_name: str = "crag-local") -> str:
    service_names = {resource.name: compose_service_name(resource) for resource in plan.resources}
    services = {}
    volumes = {}
    for resource in plan.resources:
        service_name = service_names[resource.name]
        service = {
            "image": image_for_resource(resource),
            "container_name": f"{project_name}-{service_name}",
            "environment": compose_env(resource, service_names),
            "labels": {
                "comfyui-runpod-agentic.role": resource.role,
                "comfyui-runpod-agentic.desired_hash": resource.desired_hash,
            },
        }
        ports = compose_ports(resource)
        if ports:
            service["ports"] = ports
        command = compose_command(resource, plan)
        if command:
            service["command"] = escape_compose_interpolation(command)
        volume_mount = volume_mount_for_resource(resource)
        if volume_mount:
            volume_name, mount_path, retention_policy = volume_mount
            service["volumes"] = [f"{volume_name}:{mount_path}"]
            volumes[volume_name] = {"labels": {"comfyui-runpod-agentic.retention_policy": retention_policy}}
        services[service_name] = service
    compose = {
        "name": project_name,
        "services": services,
    }
    if volumes:
        compose["volumes"] = volumes
    return yaml.safe_dump(compose, sort_keys=False)


def compose_env(resource: ResourcePlan, service_names: dict[str, str]) -> dict[str, str]:
    env = dict(resource.pod_input.get("env") or {})
    for key, value in list(env.items()):
        env[key] = resolve_local_secret_placeholder(resolve_crag_placeholders(str(value), service_names))
    if resource.role == "sql" and env.get("DATABASE_KIND") == "postgres":
        env.setdefault("POSTGRES_DB", env.get("DATABASE_NAME", "app"))
        env.setdefault("POSTGRES_USER", env.get("DATABASE_USER", "app"))
        env.setdefault("POSTGRES_PASSWORD", env.get("DATABASE_PASSWORD", "app"))
    return env


def resolve_local_secret_placeholder(value: str) -> str:
    match = re.fullmatch(r"\{\{\s*RUNPOD_SECRET_([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", value)
    if not match:
        return value
    key = match.group(1)
    return local_secret_values().get(key, value)


def local_secret_values() -> dict[str, str]:
    values: dict[str, str] = {}
    repo_root = Path(__file__).resolve().parents[1]
    paths = (
        repo_root / ".env.d/runpod.env",
        repo_root / ".env.d/ollama.env",
        Path(os.environ.get("RUNPOD_ENV_FILE", ".env.d/runpod.env")),
        Path(os.environ.get("OLLAMA_ENV_FILE", ".env.d/ollama.env")),
    )
    for path in paths:
        values.update(read_env_file(path))
    values.update(os.environ)
    return values


def resolve_crag_placeholders(value: str, service_names: dict[str, str]) -> str:
    replacements = {
        "crag://browser/neko": ("browser", "http", 8080, ""),
        "crag://browser/playwright": ("browser", "http", 3000, ""),
        "crag://llm/ollama/v1": ("llm", "http", 11434, "/v1"),
        "crag://llm/ollama": ("llm", "http", 11434, ""),
        "crag://llm/vllm/v1": ("llm", "http", 8000, "/v1"),
        "crag://vector/qdrant": ("vector", "http", 6333, ""),
        "crag://vector/chroma": ("vector", "http", 8000, ""),
        "crag://sql/postgres": ("sql", "postgresql", 5432, ""),
        "crag://sql/mysql": ("sql", "mysql", 3306, ""),
    }
    for placeholder, (role, scheme, port, suffix) in replacements.items():
        if placeholder not in value:
            continue
        service = first_service_for_role(service_names, role)
        if not service:
            continue
        value = value.replace(placeholder, f"{scheme}://{service}:{port}{suffix}")
    return value


def first_service_for_role(service_names: dict[str, str], role: str) -> str | None:
    for name, service_name in service_names.items():
        if f"-{role}-" in name or name.startswith(f"crag-{role}-"):
            return service_name
    return None


def compose_ports(resource: ResourcePlan) -> list[str]:
    ports = []
    for port in resource.ports:
        container_port = int(port.get("container_port") or port.get("privatePort") or 0)
        if not container_port or container_port == 22:
            continue
        protocol = str(port.get("protocol") or port.get("type") or "tcp").lower()
        container_protocol = "tcp" if protocol == "http" else protocol
        ports.append(f"{container_port}:{container_port}/{container_protocol}")
    return ports


def compose_command(resource: ResourcePlan, plan: DeploymentPlan) -> str | None:
    if resource.role == "agent":
        return agent_startup_command(plan, resource)
    env = resource.pod_input.get("env") or {}
    if resource.role == "llm" and env.get("LLM_PROVIDER") == "ollama":
        return "serve"
    return None


def agent_startup_command(plan: DeploymentPlan, resource: ResourcePlan) -> str:
    return "bash -lc " + shlex.quote(agent_run_script(plan, keep_container_alive=True))


def agent_run_script(plan: DeploymentPlan, *, keep_container_alive: bool = False) -> str:
    commands = [
        action.detail
        for action in plan.actions
        if action.action == "RUN_SSH_COMMAND" and action.role == "agent" and action.detail.get("phase") in {"before_start", "after_start", "after_ready"}
    ]
    script = [
        "set -euo pipefail",
        "workspace=\"${WORKSPACE_DIR:-/workspace}\"",
        "crag_dir=\"$workspace/.runpod_agentic\"",
        "mkdir -p \"$crag_dir/local-runtime\"",
        "cd \"$workspace\"",
        *local_runtime_file_writes(plan),
        "run_crag_command() {",
        "  label=\"$1\"",
        "  failure_policy=\"$2\"",
        "  retry_count=\"$3\"",
        "  body=\"$4\"",
        "  attempt=0",
        "  while true; do",
        "    echo \"[crag-local-runtime] running ${label} attempt ${attempt}\"",
        "    /bin/bash -lc \"$body\"",
        "    status=$?",
        "    if [ \"$status\" -eq 0 ]; then return 0; fi",
        "    if [ \"$failure_policy\" = \"continue\" ]; then",
        "      echo \"[crag-local-runtime] ${label} failed with ${status}; continuing\" >&2",
        "      return 0",
        "    fi",
        "    if [ \"$failure_policy\" = \"retry\" ] && [ \"$attempt\" -lt \"$retry_count\" ]; then",
        "      attempt=$((attempt + 1))",
        "      sleep 1",
        "      continue",
        "    fi",
        "    echo \"[crag-local-runtime] ${label} failed with ${status}\" >&2",
        "    return \"$status\"",
        "  done",
        "}",
    ]
    for index, command in enumerate(commands):
        label = command.get("source") or f"{command.get('phase', 'command')}:{index}"
        script.append(
            "run_crag_command "
            + " ".join(
                [
                    shlex.quote(str(label)),
                    shlex.quote(str(command.get("failure_policy") or "fail")),
                    shlex.quote(str(int(command.get("retry_count") or 0))),
                    shlex.quote(str(command.get("command") or "")),
                ]
            )
        )
    launch = launch_command_for_plan(plan)
    if launch:
        script.append("rm -f \"$crag_dir/response.txt\" \"$crag_dir/errors.txt\" \"$crag_dir/agent.log\"")
        script.append(launch)
    else:
        script.append("echo '[crag-local-runtime] startup mode is manual; launcher not started.'")
    script.append("echo '[crag-local-runtime] startup commands complete'")
    if keep_container_alive:
        script.append("sleep infinity")
    return "\n".join(script)


def local_runtime_file_writes(plan: DeploymentPlan) -> list[str]:
    agent_env = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"]
    workspace = agent_env.get("WORKSPACE_DIR", "/workspace")
    base = workspace.rstrip("/") + "/.runpod_agentic"
    commands = [action.detail for action in plan.actions if action.action == "RUN_SSH_COMMAND"]
    lines: list[str] = []
    lines.extend(shell_write_file_lines(f"{base}/resources.json", json.dumps([resource_as_runtime_json(resource) for resource in plan.resources if resource.role != "agent"], indent=2, sort_keys=True)))
    lines.extend(shell_write_file_lines(f"{base}/session.env", "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in sorted(plan.runtime_contract.env.values.items())) + "\n"))
    lines.extend(shell_write_file_lines(f"{base}/commands.json", json.dumps(commands, indent=2, sort_keys=True)))
    if plan.runtime_contract.env.values.get("AGENT_SYSTEM_PROMPT"):
        lines.extend(shell_write_file_lines(f"{base}/system_prompt.txt", plan.runtime_contract.env.values["AGENT_SYSTEM_PROMPT"]))
    if plan.runtime_contract.env.values.get("AGENT_PROMPT"):
        lines.extend(shell_write_file_lines(f"{base}/prompt.txt", plan.runtime_contract.env.values["AGENT_PROMPT"]))
    if plan.runtime_contract.env.values.get("MCP_SERVERS_JSON"):
        lines.extend(shell_write_file_lines(f"{base}/mcp_servers.json", plan.runtime_contract.env.values["MCP_SERVERS_JSON"]))
    for relative_path, content in pi_runtime_files(plan.runtime_contract.env.values).items():
        lines.extend(shell_write_file_lines(f"{base}/{relative_path}", content))
    for relative_path, content in launcher_runtime_files().items():
        lines.extend(shell_write_file_lines(f"{base}/{relative_path}", content))
    lines.append("chmod +x \"$crag_dir/launcher.sh\" \"$crag_dir\"/launcher.d/*.sh \"$crag_dir\"/launcher.d/harnesses/*.sh 2>/dev/null || true")
    return lines


def resource_as_runtime_json(resource: ResourcePlan) -> dict[str, str | None]:
    return {
        "name": resource.name,
        "role": resource.role,
        "template_id": resource.template_id,
        "materialization": resource.materialization,
    }


def shell_write_file_lines(path: str, content: str) -> list[str]:
    marker = "CRAG_LOCAL_FILE_" + re.sub(r"[^A-Z0-9_]", "_", path.upper())
    return [
        f"mkdir -p {shlex.quote(str(Path(path).parent))}",
        f"cat > {shlex.quote(path)} <<'{marker}'",
        content,
        marker,
    ]


def image_for_resource(resource: ResourcePlan) -> str:
    env = resource.pod_input.get("env") or {}
    if resource.role == "agent":
        return DEFAULT_IMAGES["agent"]
    if resource.role == "browser":
        return DEFAULT_IMAGES.get(f"browser:{env.get('BROWSER_KIND')}", DEFAULT_IMAGES["browser:playwright"])
    if resource.role == "llm":
        return DEFAULT_IMAGES.get(f"llm:{env.get('LLM_PROVIDER')}", DEFAULT_IMAGES["llm:ollama"])
    if resource.role == "sql":
        return DEFAULT_IMAGES.get(f"sql:{env.get('DATABASE_KIND')}", DEFAULT_IMAGES["sql:postgres"])
    if resource.role == "vector":
        return DEFAULT_IMAGES.get(f"vector:{env.get('VECTOR_KIND')}", DEFAULT_IMAGES["vector:qdrant"])
    return "ubuntu:24.04"


def volume_mount_for_resource(resource: ResourcePlan) -> tuple[str, str, str] | None:
    volume_id = resource.pod_input.get("networkVolumeId")
    mount_path = resource.pod_input.get("volumeMountPath")
    if not volume_id or not mount_path:
        return None
    retention_policy = resource.storage_retention_policy or "preserve"
    return (compose_service_name_from_text(str(volume_id)), str(mount_path), retention_policy)


def compose_service_name(resource: ResourcePlan) -> str:
    return compose_service_name_from_text(resource.name)


def compose_service_name_from_text(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-") or "service"


def local_runtime_summary(plan: DeploymentPlan, compose_yaml: str) -> str:
    return json.dumps(
        {
            "run_id": plan.run_id,
            "services": [compose_service_name(resource) for resource in plan.resources],
            "startup_script_preview": startup_script_for_plan(plan).splitlines()[0],
            "compose_bytes": len(compose_yaml.encode("utf-8")),
        },
        indent=2,
        sort_keys=True,
    )


def write_compose_file(path: str | Path, content: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return str(target)


def apply_compose_file(
    engine: str,
    compose_path: str | Path,
    *,
    project_name: str = "crag-local",
    action: str = "config",
    timeout_seconds: int = 1800,
) -> LocalApplyResult:
    if action not in LOCAL_RUNTIME_ACTIONS:
        raise ValueError(f"Unsupported local runtime action: {action}")
    path = str(compose_path)
    if action == "save_only":
        return LocalApplyResult(engine, action, path, [], 0, "Compose file saved; no local runtime command was run.", "")
    try:
        command = command_for_engine(engine, path, project_name, action)
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_seconds), check=False)
    except (FileNotFoundError, RuntimeError) as exc:
        return LocalApplyResult(engine, action, path, [], 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return LocalApplyResult(engine, action, path, exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], 124, exc.stdout or "", exc.stderr or str(exc))
    return LocalApplyResult(engine, action, path, command, completed.returncode, completed.stdout, completed.stderr)


def apply_local_runtime_plan(
    engine: str,
    compose_path: str | Path,
    project_name: str,
    plan: DeploymentPlan,
    *,
    action: str = "apply",
    timeout_seconds: int = 1800,
) -> tuple[LocalApplyResult, bool]:
    if action in {"apply", "apply_and_wait"} and plan.reuse_policy != "always_create":
        agent = next(resource for resource in plan.resources if resource.role == "agent")
        container_id = find_local_runtime_container(engine, project_name, "agent", desired_hash=agent.desired_hash)
        if container_id:
            return exec_agent_in_local_container(engine, project_name, container_id, plan, timeout_seconds=timeout_seconds), True
    return apply_compose_file(engine, compose_path, project_name=project_name, action=action, timeout_seconds=timeout_seconds), False


def exec_agent_in_local_container(
    engine: str,
    project_name: str,
    container_id: str,
    plan: DeploymentPlan,
    *,
    timeout_seconds: int = 1800,
) -> LocalApplyResult:
    try:
        command = local_runtime_command(engine, ["exec", container_id, "bash", "-lc", agent_run_script(plan)])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_seconds), check=False)
    except (FileNotFoundError, RuntimeError) as exc:
        return LocalApplyResult(engine, "reuse", "", [], 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return LocalApplyResult(engine, "reuse", "", exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], 124, exc.stdout or "", exc.stderr or str(exc))
    return LocalApplyResult(engine, "reuse", project_name, command, completed.returncode, completed.stdout, completed.stderr)


def enforce_local_keep_alive(
    engine: str,
    compose_path: str | Path,
    project_name: str,
    plan: DeploymentPlan,
    *,
    response_collected: bool,
) -> LocalApplyResult | None:
    policy = plan.keep_alive
    if not policy or policy.mode == "manual":
        return None
    lifecycle_action = "terminate" if policy.action == "terminate" else "stop"
    if policy.mode == "time" and policy.time_seconds:
        return schedule_local_lifecycle(engine, compose_path, project_name, lifecycle_action, int(policy.time_seconds))
    if policy.mode == "turns" and policy.turn_limit and response_collected:
        if int(policy.turn_limit) <= 1:
            return apply_compose_file(engine, compose_path, project_name=project_name, action=lifecycle_action)
        return LocalApplyResult(engine, "keep_alive", str(compose_path), [], 0, f"Local runtime turn limit is {policy.turn_limit}; current run consumed one turn and containers remain running.", "")
    if policy.mode == "cost":
        return LocalApplyResult(engine, "keep_alive", str(compose_path), [], 0, "Local runtime cannot measure provider spend; cost keep-alive was recorded but not enforced locally.", "")
    return None


def schedule_local_lifecycle(engine: str, compose_path: str | Path, project_name: str, action: str, delay_seconds: int) -> LocalApplyResult:
    try:
        command = command_for_engine(engine, str(compose_path), project_name, action)
    except RuntimeError as exc:
        return LocalApplyResult(engine, "keep_alive", str(compose_path), [], 127, "", str(exc))
    pid_path = local_keep_alive_pid_path(project_name)
    cancel_local_keep_alive(project_name)
    shell_command = f"sleep {int(delay_seconds)}; {shlex.join(command)}"
    process = subprocess.Popen(["sh", "-c", shell_command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(process.pid))
    return LocalApplyResult(engine, "keep_alive", str(compose_path), command, 0, f"Scheduled local runtime {action} in {int(delay_seconds)} seconds.", "")


def cancel_local_keep_alive(project_name: str) -> None:
    pid_path = local_keep_alive_pid_path(project_name)
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass
    pid_path.unlink(missing_ok=True)


def local_keep_alive_pid_path(project_name: str) -> Path:
    safe_project = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project_name).strip("-") or "crag-local"
    return Path(os.environ.get("CRAG_LOCAL_RUNTIME_STATE_DIR", "/tmp/crag-local-runtime")) / f"{safe_project}.keepalive.pid"


def read_local_runtime_file(
    engine: str,
    project_name: str,
    role: str,
    path: str,
    *,
    timeout_seconds: int = 120,
) -> LocalRuntimeReadResult:
    deadline = time.time() + int(timeout_seconds)
    last_stderr = ""
    command: list[str] = []
    container_id = ""
    while time.time() < deadline:
        try:
            container_id = find_local_runtime_container(engine, project_name, role) or ""
        except RuntimeError as exc:
            return LocalRuntimeReadResult(engine, project_name, role, path, "", command, 127, "", str(exc))
        if not container_id:
            last_stderr = f"No running {role} container found for project {project_name}."
            time.sleep(1)
            continue
        try:
            command = local_runtime_command(engine, ["exec", container_id, "cat", path])
        except RuntimeError as exc:
            return LocalRuntimeReadResult(engine, project_name, role, path, container_id, command, 127, "", str(exc))
        completed = subprocess.run(command, capture_output=True, text=True, timeout=min(10, int(timeout_seconds)), check=False)
        if completed.returncode == 0 and local_runtime_response_is_ready(path, completed.stdout):
            return LocalRuntimeReadResult(engine, project_name, role, path, container_id, command, completed.returncode, completed.stdout, completed.stderr)
        last_stderr = completed.stderr
        logs_result = read_local_runtime_logs(engine, project_name, role, container_id)
        if logs_result.returncode == 0 and local_runtime_logs_are_complete(logs_result.stdout):
            return LocalRuntimeReadResult(engine, project_name, role, path, container_id, logs_result.command, logs_result.returncode, logs_result.stdout, logs_result.stderr)
        time.sleep(1)
    return LocalRuntimeReadResult(engine, project_name, role, path, container_id, command, 1, "", last_stderr)


def read_local_runtime_logs(engine: str, project_name: str, role: str, container_id: str) -> LocalRuntimeReadResult:
    try:
        command = local_runtime_command(engine, ["logs", container_id])
    except RuntimeError as exc:
        return LocalRuntimeReadResult(engine, project_name, role, "<logs>", container_id, [], 127, "", str(exc))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    return LocalRuntimeReadResult(engine, project_name, role, "<logs>", container_id, command, completed.returncode, completed.stdout, completed.stderr)


def local_runtime_logs_are_complete(logs: str) -> bool:
    return any(
        marker in logs
        for marker in (
            "[crag-local-runtime] startup mode is manual; launcher not started.",
            "No compatible agent launcher was found",
        )
    )


def local_runtime_response_is_ready(path: str, text: str) -> bool:
    if path.endswith("/.runpod_agentic/response.txt"):
        return "[crag-agent] complete" in text
    return True


def find_local_runtime_container(engine: str, project_name: str, role: str, desired_hash: str | None = None) -> str | None:
    command = local_runtime_command(engine, ps_args_for_engine(engine))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        return None
    for item in parse_container_list(completed.stdout):
        name = str(item.get("Names") or item.get("Name") or item.get("NamesString") or "")
        container_id = str(item.get("ID") or item.get("Id") or item.get("ContainerID") or "")
        if not container_id or not name.startswith(f"{project_name}-"):
            continue
        labels = inspect_container_labels(engine, container_id)
        if labels.get("comfyui-runpod-agentic.role") == role and (desired_hash is None or labels.get("comfyui-runpod-agentic.desired_hash") == desired_hash):
            return container_id
    return None


def ps_args_for_engine(engine: str) -> list[str]:
    if engine == "containerd":
        return ["ps", "--format", "json"]
    if engine in {"docker", "podman"}:
        return ["ps", "--format", "{{json .}}"]
    raise RuntimeError(f"Unsupported local runtime engine: {engine}")


def parse_container_list(raw: str) -> list[dict[str, object]]:
    stripped = raw.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        data = json.loads(stripped)
        return data if isinstance(data, list) else []
    return [json.loads(line) for line in stripped.splitlines() if line.strip()]


def inspect_container_role(engine: str, container_id: str) -> str | None:
    return inspect_container_labels(engine, container_id).get("comfyui-runpod-agentic.role")


def inspect_container_labels(engine: str, container_id: str) -> dict[str, str]:
    command = local_runtime_command(engine, ["inspect", container_id])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        return {}
    data = json.loads(completed.stdout)
    inspect = data[0] if isinstance(data, list) and data else data
    labels = ((inspect.get("Config") or {}).get("Labels") or inspect.get("Labels") or {}) if isinstance(inspect, dict) else {}
    return {str(key): str(value) for key, value in labels.items()} if isinstance(labels, dict) else {}


def command_for_engine(engine: str, compose_path: str, project_name: str, action: str) -> list[str]:
    if engine == "docker":
        return compose_command_for(local_runtime_base_command(["docker", "compose"]), compose_path, project_name, action)
    if engine == "podman":
        if shutil.which("podman"):
            return compose_command_for(local_runtime_base_command(["podman", "compose"]), compose_path, project_name, action)
        raise RuntimeError("Podman local runtime requires `podman compose` on PATH.")
    if engine == "containerd":
        if not shutil.which("nerdctl"):
            raise RuntimeError("Containerd local runtime requires `nerdctl` on PATH; raw ctr does not support compose semantics.")
        return compose_command_for(local_runtime_base_command(["nerdctl", "compose"]), compose_path, project_name, action)
    raise RuntimeError(f"Unsupported local runtime engine: {engine}")


def local_runtime_command(engine: str, args: list[str]) -> list[str]:
    if engine == "docker":
        return local_runtime_base_command(["docker", *args])
    if engine == "podman":
        if not shutil.which("podman"):
            raise RuntimeError("Podman local runtime requires `podman` on PATH.")
        return local_runtime_base_command(["podman", *args])
    if engine == "containerd":
        if not shutil.which("nerdctl"):
            raise RuntimeError("Containerd local runtime requires `nerdctl` on PATH.")
        return local_runtime_base_command(["nerdctl", *args])
    raise RuntimeError(f"Unsupported local runtime engine: {engine}")


def local_runtime_base_command(command: list[str]) -> list[str]:
    sudo = ["sudo"] if use_sudo_for_local_runtime() else []
    return [*sudo, *command]


def use_sudo_for_local_runtime() -> bool:
    value = os.environ.get("CRAG_LOCAL_RUNTIME_SUDO", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def compose_command_for(base: list[str], compose_path: str, project_name: str, action: str) -> list[str]:
    command = [*base, "-f", compose_path, "-p", project_name]
    if action in {"apply", "apply_and_wait", "up"}:
        return [*command, "up", "-d"]
    if action == "stop":
        return [*command, "stop"]
    if action in {"terminate", "destroy", "down"}:
        return [*command, "down", "--remove-orphans"]
    return [*command, action]


def escape_compose_interpolation(value: str) -> str:
    return value.replace("$", "$$")
