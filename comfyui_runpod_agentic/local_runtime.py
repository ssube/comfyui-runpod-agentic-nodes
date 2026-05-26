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
from typing import Any

import yaml

from .config import read_env_file
from .planner import DeploymentPlan, ResourcePlan
from .runner import (
    keep_alive_pod_timer_script,
    launch_command_for_plan,
    launcher_runtime_files,
    pi_runtime_files,
    shell_env,
    startup_script_for_plan,
)
from .setup_commands import render_template

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

LOCAL_RUNTIME_ACTIONS = ("save_only", "plan", "apply", "apply_and_wait", "stop", "terminate")


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
                "comfyui-runpod-agentic.desired_hash": local_resource_desired_hash(resource, plan, service_names),
            },
        }
        command = compose_command(resource, plan)
        if command:
            service["command"] = escape_compose_interpolation(command)
        service_volumes = []
        volume_mount = volume_mount_for_resource(resource)
        if volume_mount:
            volume_name, mount_path, retention_policy = volume_mount
            service_volumes.append(f"{volume_name}:{mount_path}")
            volumes[volume_name] = {"labels": {"comfyui-runpod-agentic.retention_policy": retention_policy}}
        runtime_mount = local_runtime_mount_for_resource(project_name, resource)
        if runtime_mount:
            service_volumes.append(runtime_mount)
        if service_volumes:
            service["volumes"] = service_volumes
        port_mappings = local_port_mappings(resource)
        if port_mappings:
            service["ports"] = port_mappings
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
    if resource.role == "sql" and env.get("DATABASE_KIND") == "mysql":
        env.setdefault("MYSQL_DATABASE", env.get("DATABASE_NAME", "app"))
        env.setdefault("MYSQL_USER", env.get("DATABASE_USER", "app"))
        env.setdefault("MYSQL_PASSWORD", env.get("DATABASE_PASSWORD", "app"))
        env.setdefault("MYSQL_ROOT_PASSWORD", env.get("DATABASE_PASSWORD", "app"))
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
        "crag://sql/postgres/hostport": ("sql", "", 5432, ""),
        "crag://sql/postgres/host": ("sql", "", 0, ""),
        "crag://sql/mysql/hostport": ("sql", "", 3306, ""),
        "crag://sql/mysql/host": ("sql", "", 0, ""),
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
        if scheme:
            replacement = f"{scheme}://{service}:{port}{suffix}"
        elif port:
            replacement = f"{service}:{port}{suffix}"
        else:
            replacement = f"{service}{suffix}"
        value = value.replace(placeholder, replacement)
    return value


def first_service_for_role(service_names: dict[str, str], role: str) -> str | None:
    for name, service_name in service_names.items():
        if f"-{role}-" in name or name.startswith(f"crag-{role}-"):
            return service_name
    return None


def compose_command(resource: ResourcePlan, plan: DeploymentPlan) -> str | None:
    if resource.role == "agent":
        return agent_startup_command(plan, resource)
    env = resource.pod_input.get("env") or {}
    if resource.role == "llm" and env.get("LLM_PROVIDER") == "ollama":
        return "serve"
    return None


def local_resource_desired_hash(resource: ResourcePlan, plan: DeploymentPlan, service_names: dict[str, str] | None = None) -> str:
    service_names = service_names or {item.name: compose_service_name(item) for item in plan.resources}
    env = compose_env(resource, service_names)
    command_shape: object = compose_command(resource, plan)
    if resource.role == "agent":
        env = stable_agent_env(env)
        command_shape = stable_agent_command_shape(plan)
    return stable_local_hash(
        {
            "image": image_for_resource(resource),
            "environment": env,
            "command": command_shape,
            "ports": local_port_mappings(resource),
            "volume_mount": volume_mount_for_resource(resource),
        }
    )[:12]


def stable_agent_env(env: dict[str, str]) -> dict[str, str]:
    transient = {
        "AGENT_PROMPT",
        "CRAG_RUN_ID",
        "CRAG_WORKFLOW_HASH",
    }
    return {key: value for key, value in env.items() if key not in transient}


def stable_agent_command_shape(plan: DeploymentPlan) -> dict[str, object]:
    return {
        "before_start": local_runtime_commands_for_phase(plan, {"before_start"}),
        "after_start": local_runtime_commands_for_phase(plan, {"after_start", "after_ready"}),
        "keep_container_alive": True,
        "launcher": launch_command_for_plan(plan),
    }


def stable_local_hash(value: object) -> str:
    import hashlib

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def agent_startup_command(plan: DeploymentPlan, resource: ResourcePlan) -> str:
    workspace = resource.pod_input.get("env", {}).get("WORKSPACE_DIR", "/workspace")
    return "bash -lc " + shlex.quote(f"bash {shlex.quote(workspace.rstrip('/') + '/.runpod_agentic/local-runtime/run-agent.sh')}")


def agent_run_script(plan: DeploymentPlan, *, keep_container_alive: bool = False) -> str:
    before_commands = local_runtime_commands_for_phase(plan, {"before_start"})
    after_commands = local_runtime_commands_for_phase(plan, {"after_start", "after_ready"})
    before_lines = []
    for index, command in enumerate(before_commands):
        label = command.get("source") or f"{command.get('phase', 'command')}:{index}"
        before_lines.append(
            "run_crag_command "
            + " ".join(
                [
                    shlex.quote(str(label)),
                    shlex.quote(str(command.get("failure_policy") or "fail")),
                    shlex.quote(str(int(command.get("retry_count") or 0))),
                    f"\"$crag_dir/local-runtime/commands/{local_runtime_command_filename(index, command)}\"",
                ]
            )
        )
    launch = launch_command_for_plan(plan)
    if launch:
        launch_block = "\n".join(['rm -f "$crag_dir/response.txt" "$crag_dir/errors.txt" "$crag_dir/agent.log"', launch])
    else:
        launch_block = "echo '[crag-local-runtime] startup mode is manual; launcher not started.'"
    after_lines = []
    for index, command in enumerate(after_commands):
        label = command.get("source") or f"{command.get('phase', 'command')}:{index}"
        command_index = len(before_commands) + index
        after_lines.append(
            "run_crag_command "
            + " ".join(
                [
                    shlex.quote(str(label)),
                    shlex.quote(str(command.get("failure_policy") or "fail")),
                    shlex.quote(str(int(command.get("retry_count") or 0))),
                    f"\"$crag_dir/local-runtime/commands/{local_runtime_command_filename(command_index, command)}\"",
                ]
            )
        )
    keep_alive_lines = []
    if keep_container_alive:
        keep_alive_lines.extend(local_runtime_self_shutdown_lines(plan))
        keep_alive_lines.append("sleep infinity")
    return render_template(
        "runtime/local-agent-run.sh.j2",
        {
            "before_commands": "\n".join(before_lines),
            "launch_block": launch_block,
            "after_commands": "\n".join(after_lines),
            "keep_alive_block": "\n".join(keep_alive_lines),
        },
    )


def local_runtime_command_filename(index: int, command: dict[str, Any]) -> str:
    phase = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(command.get("phase") or "command")).strip("-") or "command"
    return f"{index:04d}-{phase}.sh"


def local_runtime_commands_for_phase(plan: DeploymentPlan, phases: set[str]) -> list[dict[str, Any]]:
    return [
        action.detail
        for action in plan.actions
        if action.action == "RUN_SSH_COMMAND" and action.role == "agent" and action.detail.get("phase") in phases
    ]


def local_runtime_self_shutdown_lines(plan: DeploymentPlan) -> list[str]:
    script = keep_alive_pod_timer_script(plan.keep_alive)
    if not script:
        return []
    return [f"bash -lc {shell_env(script)}"]


def local_runtime_file_contents(plan: DeploymentPlan, service_names: dict[str, str] | None = None, *, keep_container_alive: bool = True) -> dict[str, str]:
    service_names = service_names or {resource.name: compose_service_name(resource) for resource in plan.resources}
    agent_env = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"]
    workspace = agent_env.get("WORKSPACE_DIR", "/workspace")
    base = workspace.rstrip("/") + "/.runpod_agentic"
    commands = [action.detail for action in plan.actions if action.action == "RUN_SSH_COMMAND"]
    resolved_env = {
        key: resolve_local_secret_placeholder(resolve_crag_placeholders(str(value), service_names))
        for key, value in sorted(plan.runtime_contract.env.values.items())
    }
    files: dict[str, str] = {}
    files["resources.json"] = json.dumps([resource_as_runtime_json(resource) for resource in plan.resources if resource.role != "agent"], indent=2, sort_keys=True)
    files["session.env"] = "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in resolved_env.items()) + "\n"
    files["commands.json"] = json.dumps(commands, indent=2, sort_keys=True)
    if resolved_env.get("AGENT_SYSTEM_PROMPT"):
        files["system_prompt.txt"] = resolved_env["AGENT_SYSTEM_PROMPT"]
    if resolved_env.get("AGENT_PROMPT"):
        files["prompt.txt"] = resolved_env["AGENT_PROMPT"]
    if resolved_env.get("MCP_SERVERS_JSON"):
        files["mcp_servers.json"] = resolved_env["MCP_SERVERS_JSON"]
    for relative_path, content in plan.runtime_contract.files.items():
        normalized = "/" + relative_path.strip("/")
        if normalized.startswith(base.rstrip("/") + "/"):
            files[normalized.removeprefix(base.rstrip("/") + "/")] = content
    for relative_path, content in pi_runtime_files(resolved_env).items():
        files[relative_path] = content
    for relative_path, content in launcher_runtime_files().items():
        files[relative_path] = content
    files["local-runtime/run-agent.sh"] = agent_run_script(plan, keep_container_alive=keep_container_alive)
    for index, command in enumerate(commands):
        files[f"local-runtime/commands/{local_runtime_command_filename(index, command)}"] = "#!/usr/bin/env bash\n" + str(command.get("command") or "") + "\n"
    return files


def local_runtime_file_writes(plan: DeploymentPlan, service_names: dict[str, str] | None = None) -> list[str]:
    agent_env = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"]
    workspace = agent_env.get("WORKSPACE_DIR", "/workspace")
    base = workspace.rstrip("/") + "/.runpod_agentic"
    lines: list[str] = []
    for relative_path, content in local_runtime_file_contents(plan, service_names).items():
        lines.extend(shell_write_file_lines(f"{base}/{relative_path}", content))
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
    if resource.pod_input.get("imageName"):
        return str(resource.pod_input["imageName"])
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
    volume_id = resource.pod_input.get("networkVolumeId") or resource.pod_input.get("_networkVolumeName")
    mount_path = resource.pod_input.get("volumeMountPath")
    if not volume_id or not mount_path:
        return None
    retention_policy = resource.storage_retention_policy or "preserve"
    return (compose_service_name_from_text(str(volume_id)), str(mount_path), retention_policy)


def local_runtime_mount_for_resource(project_name: str, resource: ResourcePlan) -> str | None:
    if resource.role != "agent":
        return None
    workspace = resource.pod_input.get("env", {}).get("WORKSPACE_DIR", "/workspace")
    return f"{local_runtime_project_dir(project_name)}/runtime:{workspace.rstrip('/')}/.runpod_agentic"


def local_runtime_project_dir(project_name: str) -> Path:
    safe_project = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project_name).strip("-") or "crag-local"
    return Path(os.environ.get("CRAG_LOCAL_RUNTIME_STATE_DIR", "/tmp/crag-local-runtime")) / safe_project


def write_local_runtime_files(plan: DeploymentPlan, project_name: str) -> Path:
    service_names = {resource.name: compose_service_name(resource) for resource in plan.resources}
    runtime_dir = local_runtime_project_dir(project_name) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in local_runtime_file_contents(plan, service_names).items():
        target = runtime_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if relative_path.endswith(".sh"):
            target.chmod(0o755)
    return runtime_dir


def clear_local_runtime_agent_outputs(project_name: str) -> None:
    runtime_dir = local_runtime_project_dir(project_name) / "runtime"
    for relative_path in ("response.txt", "errors.txt", "agent.log", "startup.ready"):
        try:
            (runtime_dir / relative_path).unlink()
        except FileNotFoundError:
            pass


def local_port_mappings(resource: ResourcePlan) -> list[str]:
    if resource.role != "agent":
        return []
    env = resource.pod_input.get("env") or {}
    if env.get("CRAG_WEB_TERMINAL") != "1":
        return []
    container_port = int(env.get("CRAG_WEB_TERMINAL_PORT") or 7681)
    host_port = int(env.get("CRAG_WEB_TERMINAL_HOST_PORT") or container_port)
    if host_port <= 0:
        return []
    return [f"127.0.0.1:{host_port}:{container_port}"]


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


def local_runtime_summary_from_file(compose_path: str | Path) -> str:
    path = Path(compose_path)
    compose = yaml.safe_load(path.read_text()) or {}
    services = sorted((compose.get("services") or {}).keys())
    return json.dumps(
        {
            "compose_path": str(path),
            "services": services,
            "service_count": len(services),
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
    action: str = "plan",
    timeout_seconds: int = 1800,
) -> LocalApplyResult:
    if action not in LOCAL_RUNTIME_ACTIONS:
        raise ValueError(f"Unsupported local runtime action: {action}")
    path = str(compose_path)
    if action == "save_only":
        return LocalApplyResult(engine, action, path, [], 0, "Compose file saved; no local runtime command was run.", "")
    if action == "plan":
        return LocalApplyResult(engine, action, path, [], 0, local_runtime_summary_from_file(path), "")
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
    cleanup: LocalApplyResult | None = None
    if action in {"save_only", "plan", "apply", "apply_and_wait"}:
        write_local_runtime_files(plan, project_name)
    if action in {"apply", "apply_and_wait"}:
        clear_local_runtime_agent_outputs(project_name)
    if action in {"apply", "apply_and_wait"} and plan.reuse_policy != "always_create":
        agent = next(resource for resource in plan.resources if resource.role == "agent")
        container_id = find_local_runtime_container(engine, project_name, "agent", desired_hash=local_resource_desired_hash(agent, plan)) if all_local_runtime_resources_running(engine, project_name, plan) else None
        if container_id:
            write_local_runtime_files(plan, project_name)
            return exec_agent_in_local_container(engine, project_name, container_id, plan, timeout_seconds=timeout_seconds), True
        if plan.reuse_policy == "resume_stopped" and all_local_runtime_resources_exist(engine, project_name, plan):
            start_result = start_local_runtime_project(engine, compose_path, project_name, timeout_seconds=timeout_seconds)
            if start_result.returncode != 0:
                return start_result, False
            container_id = find_local_runtime_container(engine, project_name, "agent", desired_hash=local_resource_desired_hash(agent, plan))
            if container_id:
                write_local_runtime_files(plan, project_name)
                exec_result = exec_agent_in_local_container(engine, project_name, container_id, plan, timeout_seconds=timeout_seconds)
                return LocalApplyResult(
                    exec_result.engine,
                    "resume_stopped",
                    exec_result.compose_path,
                    [*start_result.command, "&&", *exec_result.command] if start_result.command and exec_result.command else exec_result.command or start_result.command,
                    exec_result.returncode,
                    "\n".join(part for part in (start_result.stdout, exec_result.stdout) if part),
                    "\n".join(part for part in (start_result.stderr, exec_result.stderr) if part),
                ), True
        cleanup = reconcile_stale_local_runtime_project(engine, compose_path, project_name, plan, timeout_seconds=timeout_seconds)
        if cleanup and cleanup.returncode != 0:
            return cleanup, False
    elif action in {"apply", "apply_and_wait"}:
        cleanup = reconcile_stale_local_runtime_project(engine, compose_path, project_name, plan, timeout_seconds=timeout_seconds)
        if cleanup and cleanup.returncode != 0:
            return cleanup, False
    result = apply_compose_file(engine, compose_path, project_name=project_name, action=action, timeout_seconds=timeout_seconds)
    if action == "stop" and result.returncode == 0:
        stop_result = stop_local_runtime_project_containers(engine, project_name, timeout_seconds=timeout_seconds)
        if stop_result:
            result = LocalApplyResult(
                result.engine,
                result.action,
                result.compose_path,
                [*result.command, "&&", *stop_result.command] if result.command and stop_result.command else result.command or stop_result.command,
                stop_result.returncode,
                "\n".join(part for part in (result.stdout, stop_result.stdout) if part),
                "\n".join(part for part in (result.stderr, stop_result.stderr) if part),
            )
    if cleanup:
        result = LocalApplyResult(
            result.engine,
            result.action,
            result.compose_path,
            [*cleanup.command, "&&", *result.command] if cleanup.command and result.command else result.command or cleanup.command,
            result.returncode,
            "\n".join(part for part in (cleanup.stdout, result.stdout) if part),
            "\n".join(part for part in (cleanup.stderr, result.stderr) if part),
        )
    if action in {"apply", "apply_and_wait"} and result.returncode == 0 and should_wait_local_startup(plan):
        startup = wait_local_runtime_startup_complete(engine, project_name, "agent", plan, timeout_seconds=timeout_seconds)
        if startup.returncode != 0:
            return LocalApplyResult(result.engine, result.action, result.compose_path, result.command, startup.returncode, result.stdout, "\n".join(part for part in (result.stderr, startup.stderr) if part)), False
    if action == "terminate" and result.returncode == 0:
        cleanup = remove_local_retention_volumes(engine, project_name, plan, timeout_seconds=timeout_seconds)
        if cleanup:
            result = LocalApplyResult(
                result.engine,
                result.action,
                result.compose_path,
                [*result.command, "&&", *cleanup.command] if result.command else cleanup.command,
                cleanup.returncode,
                "\n".join(part for part in (result.stdout, cleanup.stdout) if part),
                "\n".join(part for part in (result.stderr, cleanup.stderr) if part),
            )
    return result, False


def reconcile_stale_local_runtime_project(
    engine: str,
    compose_path: str | Path,
    project_name: str,
    plan: DeploymentPlan,
    *,
    timeout_seconds: int = 1800,
) -> LocalApplyResult | None:
    if not list_local_runtime_project_containers(engine, project_name):
        return None
    if all_local_runtime_resources_running(engine, project_name, plan):
        return None
    return apply_compose_file(engine, compose_path, project_name=project_name, action="terminate", timeout_seconds=timeout_seconds)


def remove_local_retention_volumes(engine: str, project_name: str, plan: DeploymentPlan, *, timeout_seconds: int = 1800) -> LocalApplyResult | None:
    volume_names = local_retention_volume_names(project_name, plan)
    if not volume_names:
        return None
    try:
        command = local_runtime_command(engine, ["volume", "rm", *volume_names])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_seconds), check=False)
    except (FileNotFoundError, RuntimeError) as exc:
        return LocalApplyResult(engine, "volume_cleanup", "", [], 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return LocalApplyResult(engine, "volume_cleanup", "", exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], 124, exc.stdout or "", exc.stderr or str(exc))
    return LocalApplyResult(engine, "volume_cleanup", "", command, completed.returncode, completed.stdout, completed.stderr)


def stop_local_runtime_project_containers(engine: str, project_name: str, *, timeout_seconds: int = 1800) -> LocalApplyResult | None:
    container_ids = list_local_runtime_project_containers(engine, project_name, only_running=True)
    if not container_ids:
        return None
    try:
        command = local_runtime_command(engine, ["stop", *container_ids])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_seconds), check=False)
    except (FileNotFoundError, RuntimeError) as exc:
        return LocalApplyResult(engine, "stop", "", [], 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return LocalApplyResult(engine, "stop", "", exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], 124, exc.stdout or "", exc.stderr or str(exc))
    return LocalApplyResult(engine, "stop", "", command, completed.returncode, completed.stdout, completed.stderr)


def start_local_runtime_project(engine: str, compose_path: str | Path, project_name: str, *, timeout_seconds: int = 1800) -> LocalApplyResult:
    try:
        command = [*command_for_engine(engine, str(compose_path), project_name, "plan")[:-1], "start"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(timeout_seconds), check=False)
    except (FileNotFoundError, RuntimeError) as exc:
        return LocalApplyResult(engine, "resume_stopped", str(compose_path), [], 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return LocalApplyResult(engine, "resume_stopped", str(compose_path), exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], 124, exc.stdout or "", exc.stderr or str(exc))
    return LocalApplyResult(engine, "resume_stopped", str(compose_path), command, completed.returncode, completed.stdout, completed.stderr)


def local_retention_volume_names(project_name: str, plan: DeploymentPlan) -> list[str]:
    names = []
    for resource in plan.resources:
        if resource.storage_retention_policy in {None, "preserve"}:
            continue
        volume_id = resource.pod_input.get("networkVolumeId")
        mount_path = resource.pod_input.get("volumeMountPath")
        if volume_id and mount_path:
            names.append(f"{project_name}_{compose_service_name_from_text(str(volume_id))}")
    return sorted(set(names))


def all_local_runtime_resources_running(engine: str, project_name: str, plan: DeploymentPlan) -> bool:
    return all(find_local_runtime_container(engine, project_name, resource.role, desired_hash=local_resource_desired_hash(resource, plan)) for resource in plan.resources)


def all_local_runtime_resources_exist(engine: str, project_name: str, plan: DeploymentPlan) -> bool:
    return all(find_local_runtime_project_container(engine, project_name, resource.role, desired_hash=local_resource_desired_hash(resource, plan)) for resource in plan.resources)


def plan_has_web_terminal(plan: DeploymentPlan) -> bool:
    agent = next((resource for resource in plan.resources if resource.role == "agent"), None)
    env = (agent.pod_input.get("env") if agent else {}) or {}
    return env.get("CRAG_WEB_TERMINAL") == "1"


def wait_local_runtime_startup_complete(
    engine: str,
    project_name: str,
    role: str,
    plan: DeploymentPlan,
    *,
    timeout_seconds: int = 1800,
) -> LocalRuntimeReadResult:
    deadline = time.time() + int(timeout_seconds)
    command: list[str] = []
    last_stderr = ""
    container_id = ""
    desired_hash = local_resource_desired_hash(next(resource for resource in plan.resources if resource.role == role), plan)
    while time.time() < deadline:
        container_id = find_local_runtime_container(engine, project_name, role, desired_hash=desired_hash) or ""
        if not container_id:
            last_stderr = f"No running {role} container found for project {project_name}."
            time.sleep(1)
            continue
        logs_result = read_local_runtime_logs(engine, project_name, role, container_id)
        command = logs_result.command
        if logs_result.returncode == 0 and local_runtime_logs_are_complete(logs_result.stdout):
            return logs_result
        last_stderr = logs_result.stderr
        time.sleep(1)
    return LocalRuntimeReadResult(engine, project_name, role, "<logs>", container_id, command, 1, "", last_stderr or "Timed out waiting for local runtime startup commands to complete.")


def should_wait_local_startup(plan: DeploymentPlan) -> bool:
    policy = plan.keep_alive
    return plan_has_web_terminal(plan) or bool(policy and policy.mode == "time" and policy.time_seconds and policy.enforcement in {"server_side", "both"})


def exec_agent_in_local_container(
    engine: str,
    project_name: str,
    container_id: str,
    plan: DeploymentPlan,
    *,
    timeout_seconds: int = 1800,
) -> LocalApplyResult:
    try:
        workspace = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"].get("WORKSPACE_DIR", "/workspace")
        command = local_runtime_command(engine, ["exec", container_id, "bash", "-lc", f"bash {shlex.quote(workspace.rstrip('/') + '/.runpod_agentic/local-runtime/run-agent.sh')}"])
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
    if policy.enforcement not in {"server_side", "both"}:
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
    requires_response_file = local_runtime_path_requires_ready_response(path)
    while time.time() < deadline:
        try:
            container_id = find_local_runtime_container(engine, project_name, role) or ""
        except RuntimeError as exc:
            return LocalRuntimeReadResult(engine, project_name, role, path, "", command, 127, "", str(exc))
        if not container_id:
            stopped_container_id = find_local_runtime_project_container(engine, project_name, role)
            if stopped_container_id:
                logs_result = read_local_runtime_logs(engine, project_name, role, stopped_container_id)
                return LocalRuntimeReadResult(
                    engine,
                    project_name,
                    role,
                    path,
                    stopped_container_id,
                    logs_result.command,
                    1,
                    "",
                    logs_result.stdout or logs_result.stderr or f"{role} container exited before {path} was ready.",
                )
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
            if requires_response_file:
                last_stderr = logs_result.stdout or logs_result.stderr or f"{path} was not ready after the {role} container completed."
                time.sleep(1)
                continue
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
            "[crag-local-runtime] startup commands complete",
            "[crag-local-runtime] startup mode is manual; launcher not started.",
            "No compatible agent launcher was found",
        )
    )


def local_runtime_response_is_ready(path: str, text: str) -> bool:
    if local_runtime_path_requires_ready_response(path):
        return "[crag-agent] complete" in text
    return True


def local_runtime_path_requires_ready_response(path: str) -> bool:
    return path.endswith("/.runpod_agentic/response.txt")


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


def list_local_runtime_project_containers(engine: str, project_name: str, *, only_running: bool = False) -> list[str]:
    command = local_runtime_command(engine, ps_args_for_engine(engine, all_containers=not only_running))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        return []
    containers = []
    for item in parse_container_list(completed.stdout):
        name = str(item.get("Names") or item.get("Name") or item.get("NamesString") or "")
        container_id = str(item.get("ID") or item.get("Id") or item.get("ContainerID") or "")
        if container_id and name.startswith(f"{project_name}-"):
            containers.append(container_id)
    return containers


def find_local_runtime_project_container(engine: str, project_name: str, role: str, desired_hash: str | None = None) -> str | None:
    for container_id in list_local_runtime_project_containers(engine, project_name):
        labels = inspect_container_labels(engine, container_id)
        if labels.get("comfyui-runpod-agentic.role") == role and (desired_hash is None or labels.get("comfyui-runpod-agentic.desired_hash") == desired_hash):
            return container_id
    return None


def ps_args_for_engine(engine: str, *, all_containers: bool = False) -> list[str]:
    if engine == "containerd":
        return ["ps", *(["-a"] if all_containers else []), "--format", "json"]
    if engine in {"docker", "podman"}:
        return ["ps", *(["-a"] if all_containers else []), "--format", "{{json .}}"]
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
    if action in {"apply", "apply_and_wait"}:
        return [*command, "up", "-d"]
    if action == "stop":
        return [*command, "stop"]
    if action == "terminate":
        return [*command, "down", "--remove-orphans"]
    return [*command, action]


def escape_compose_interpolation(value: str) -> str:
    return value.replace("$", "$$")
