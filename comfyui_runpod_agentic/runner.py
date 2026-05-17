from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .planner import DeploymentPlan, Planner
from .runpod_client import RunpodClient, RunpodClientProtocol
from .runtime_contracts import with_env
from .specs import DeploymentSpec, RuntimeContract, to_plain
from .ssh_client import SSHClientProtocol, SSHConfig, SubprocessSSHClient, extract_ssh_endpoint, runpod_proxy_ssh_endpoint
from .state_store import StateStore
from .validation import validate_deployment


def default_state_path() -> Path:
    return Path(os.environ.get("COMFYUI_USER_DIR", "user")) / "runpod-agentic" / "state.sqlite"


@dataclass
class RunpodRunner:
    runpod_client: RunpodClientProtocol | None = None
    ssh_client: SSHClientProtocol | None = None
    state_store: StateStore | None = None
    planner: Planner | None = None

    def __post_init__(self) -> None:
        if self.runpod_client is None:
            self.runpod_client = RunpodClient()
        if self.ssh_client is None:
            self.ssh_client = SubprocessSSHClient()
        if self.state_store is None:
            self.state_store = StateStore(default_state_path())
        if self.planner is None:
            self.planner = Planner()

    def run(self, deployment: DeploymentSpec, *, mode: str, prompt: Any = None, on_error: str = "stop_created") -> dict[str, Any]:
        validate_deployment(deployment, mode=mode, require_api_key=True)
        plan = self.planner.build(deployment, mode=mode, prompt=prompt)
        self.reconcile_managed_pods()
        self.state_store.record_run(plan.run_id, plan.workflow_hash, plan.deployment_hash, mode, "started")
        try:
            if mode in {"stop", "terminate", "destroy"}:
                return self._lifecycle(plan, mode)
            result = self._apply(plan, wait=mode == "apply_and_wait")
            self.state_store.record_run(plan.run_id, plan.workflow_hash, plan.deployment_hash, mode, "completed")
            return result
        except Exception as exc:
            self.state_store.add_event(plan.run_id, "error", str(exc))
            self.state_store.record_run(plan.run_id, plan.workflow_hash, plan.deployment_hash, mode, "failed")
            if on_error != "leave_running":
                self._cleanup_created(plan, terminate=on_error == "terminate_created")
            raise

    def _apply(self, plan: DeploymentPlan, *, wait: bool) -> dict[str, Any]:
        created: dict[str, dict[str, Any]] = {}
        resource_ids: dict[str, str] = {}
        for resource in plan.resources:
            pod = self.runpod_client.create_or_deploy_pod(resource.pod_input)
            created[resource.name] = pod
            resource_id = self.state_store.record_resource(plan.run_id, resource, pod, status=pod.get("desiredStatus", "created"))
            resource_ids[resource.name] = resource_id
            self.state_store.add_event(plan.run_id, "pod_created", resource.name, resource_id=resource_id, payload={"pod_id": pod.get("id")})

        agent = next(resource for resource in plan.resources if resource.role == "agent")
        resolved_contract = resolve_dependency_endpoints(plan, created)
        plan = replace(plan, runtime_contract=resolved_contract)
        agent_pod = created[agent.name]
        host, port = self._wait_ssh_endpoint(agent_pod, plan)
        self._wait_ssh_ready(host, port)
        self._run_agent_ssh_steps(plan, host, port, resource_ids.get(agent.name))
        status = "waiting" if wait else "launched"
        return {"run_id": plan.run_id, "status": status, "pods": {name: pod.get("id") for name, pod in created.items()}, "plan": plan.to_dict()}

    def _run_agent_ssh_steps(self, plan: DeploymentPlan, host: str, port: int, resource_id: str | None) -> None:
        commands = [action for action in plan.actions if action.action == "RUN_SSH_COMMAND"]
        for action in commands:
            command = action.detail["command"]
            log_paths = self._command_log_paths(plan.run_id, action.detail)
            command_id = self.state_store.start_command(
                plan.run_id,
                resource_id,
                action.detail["phase"],
                int(action.detail["order"]),
                action.detail.get("command_hash") or stable_command_hash(command),
                str(log_paths["stdout"]),
                str(log_paths["stderr"]),
            )
            result = self.ssh_client.run(host, port, command)
            log_paths["stdout"].write_text(result.stdout)
            log_paths["stderr"].write_text(result.stderr)
            status = "completed" if result.exit_code == 0 else "failed"
            self.state_store.finish_command(command_id, status, result.exit_code)
            self.state_store.add_event(plan.run_id, "ssh_command", command, payload={"exit_code": result.exit_code})
            if result.exit_code != 0 and action.detail.get("failure_policy") == "fail":
                raise RuntimeError(f"SSH command failed with exit code {result.exit_code}: {command}")
        self._write_runtime_files(plan, host, port)
        launch = self._launch_command(plan)
        if not launch:
            self.state_store.add_event(plan.run_id, "agent_launch_skipped", "manual startup mode")
            return
        result = self.ssh_client.run(host, port, launch)
        self.state_store.add_event(plan.run_id, "agent_launch", launch, payload={"exit_code": result.exit_code})
        if result.exit_code != 0:
            raise RuntimeError(f"Agent launch failed with exit code {result.exit_code}.")

    def _write_runtime_files(self, plan: DeploymentPlan, host: str, port: int) -> None:
        workspace = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"].get("WORKSPACE_DIR", "/workspace")
        base = workspace.rstrip("/") + "/.runpod_agentic"
        resources = {"resources": [to_plain(resource) for resource in plan.resources if resource.role != "agent"]}
        session_env = "\n".join(f"export {key}={shell_env(value)}" for key, value in sorted(plan.runtime_contract.env.values.items())) + "\n"
        commands = [action.detail for action in plan.actions if action.action == "RUN_SSH_COMMAND"]
        self.ssh_client.write_file(host, port, f"{base}/resources.json", json.dumps(resources, indent=2, sort_keys=True))
        self.ssh_client.write_file(host, port, f"{base}/session.env", session_env)
        self.ssh_client.write_file(host, port, f"{base}/commands.json", json.dumps(commands, indent=2, sort_keys=True))
        self.state_store.add_event(plan.run_id, "runtime_config_written", base)

    def _command_log_paths(self, run_id: str, detail: dict[str, Any]) -> dict[str, Path]:
        base = self.state_store.path.parent / "logs" / run_id
        base.mkdir(parents=True, exist_ok=True)
        prefix = f"{int(detail['order']):04d}-{detail['phase']}-{stable_command_hash(detail['command'])[:12]}"
        return {"stdout": base / f"{prefix}.stdout.log", "stderr": base / f"{prefix}.stderr.log"}

    def _wait_ssh_endpoint(self, pod: dict[str, Any], plan: DeploymentPlan, timeout_seconds: int = 180, interval_seconds: int = 5) -> tuple[str, int]:
        pod_id = pod.get("id")
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if plan.ssh_access.mode == "runpod_proxy":
                try:
                    self._configure_ssh_client(plan)
                    return runpod_proxy_ssh_endpoint(pod, plan.ssh_access.proxy_key_suffix)
                except Exception as exc:
                    last_error = exc
            try:
                return extract_ssh_endpoint(pod)
            except Exception as exc:
                last_error = exc
            if pod_id:
                pod = self.runpod_client.get_pod(pod_id)
            time.sleep(interval_seconds)
        raise RuntimeError(f"Timed out waiting for SSH endpoint on pod {pod_id}: {last_error}")

    def _configure_ssh_client(self, plan: DeploymentPlan) -> None:
        if isinstance(self.ssh_client, SubprocessSSHClient):
            self.ssh_client.config = SSHConfig(username=plan.ssh_access.username, private_key_path=plan.ssh_access.private_key_path)

    def _wait_ssh_ready(self, host: str, port: int, timeout_seconds: int = 180, interval_seconds: int = 5) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_result: Any = None
        while time.monotonic() < deadline:
            last_result = self.ssh_client.run(host, port, "true", timeout_seconds=20)
            if last_result.exit_code == 0:
                return
            time.sleep(interval_seconds)
        stderr = getattr(last_result, "stderr", "")
        raise RuntimeError(f"Timed out waiting for SSH readiness on {host}:{port}: {stderr}")

    def _launch_command(self, plan: DeploymentPlan) -> str:
        agent_env = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"]
        if agent_env.get("AGENT_STARTUP_MODE") == "manual":
            return ""
        workspace = agent_env.get("WORKSPACE_DIR", "/workspace")
        harness = agent_env.get("AGENT_HARNESS", "agent")
        return f"cd {workspace} && test -x /usr/local/bin/runpod-agent-launch && nohup /usr/local/bin/runpod-agent-launch {harness} > .runpod_agentic/agent.log 2>&1 &"

    def _lifecycle(self, plan: DeploymentPlan, mode: str) -> dict[str, Any]:
        resources = self.state_store.list_resources()
        matched = [resource for resource in resources if resource.get("desired_hash") in {item.desired_hash for item in plan.resources}]
        action = "terminate" if mode in {"terminate", "destroy"} else "stop"
        for resource in matched:
            pod_id = resource.get("runpod_pod_id")
            if not pod_id:
                continue
            if action == "terminate":
                self.runpod_client.terminate_pod(pod_id)
            else:
                self.runpod_client.stop_pod(pod_id)
        return {"run_id": plan.run_id, "status": action, "resources": matched}

    def _cleanup_created(self, plan: DeploymentPlan, *, terminate: bool) -> None:
        for resource in self.state_store.list_resources():
            if resource.get("run_id") != plan.run_id or not resource.get("runpod_pod_id"):
                continue
            if terminate:
                self.runpod_client.terminate_pod(resource["runpod_pod_id"])
            else:
                self.runpod_client.stop_pod(resource["runpod_pod_id"])

    def reconcile_managed_pods(self, run_id: str | None = None) -> list[dict[str, Any]]:
        pods = [pod for pod in self.runpod_client.list_pods() if str(pod.get("name", "")).startswith("crag-")]
        for pod in pods:
            self.state_store.record_remote_resource(pod, run_id=run_id)
        return pods


def shell_env(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def stable_command_hash(command: str) -> str:
    import hashlib

    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def resolve_dependency_endpoints(plan: DeploymentPlan, pods: dict[str, dict[str, Any]]) -> RuntimeContract:
    env = dict(plan.runtime_contract.env.values)
    for resource in plan.resources:
        if resource.role == "agent":
            continue
        endpoint = public_http_endpoint(pods.get(resource.name, {}))
        if not endpoint:
            continue
        if resource.role == "browser":
            replace_prefix(env, "crag://browser/playwright", endpoint)
            replace_prefix(env, "crag://browser/neko", endpoint)
        elif resource.role == "llm":
            replace_prefix(env, "crag://llm/ollama", endpoint)
            replace_prefix(env, "crag://llm/vllm", endpoint)
        elif resource.role == "sql":
            replace_prefix(env, "crag://sql/postgres", endpoint)
            replace_prefix(env, "crag://sql/mysql", endpoint)
        elif resource.role == "vector":
            replace_prefix(env, "crag://vector/chroma", endpoint)
            replace_prefix(env, "crag://vector/qdrant", endpoint)
    return with_env(plan.runtime_contract, env)


def replace_prefix(env: dict[str, str], placeholder: str, endpoint: str) -> None:
    for key, value in list(env.items()):
        if value.startswith(placeholder):
            env[key] = endpoint + value.removeprefix(placeholder)


def public_http_endpoint(pod: dict[str, Any]) -> str | None:
    ports = ((pod.get("runtime") or {}).get("ports") or pod.get("ports") or [])
    for port in ports:
        private = port.get("privatePort") or port.get("containerPort") or port.get("container_port")
        if int(private or 0) == 22:
            continue
        public = port.get("publicPort") or port.get("public_port")
        host = port.get("ip") or port.get("host") or port.get("hostname")
        if host and public:
            scheme = "https" if str(port.get("type") or port.get("protocol")).lower() == "https" else "http"
            return f"{scheme}://{host}:{int(public)}"
    return None
