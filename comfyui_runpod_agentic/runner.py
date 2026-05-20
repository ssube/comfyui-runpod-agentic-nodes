from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .planner import DeploymentPlan, Planner
from .runpod_client import RunpodClient, RunpodClientProtocol
from .runtime_contracts import with_env
from .specs import DeploymentSpec, KeepAlivePolicy, RuntimeContract, to_plain
from .ssh_client import SSHClientProtocol, SSHConfig, SubprocessSSHClient, extract_ssh_endpoint, runpod_proxy_ssh_endpoint
from .state_store import StateStore
from .validation import validate_deployment


def default_state_path() -> Path:
    return Path(os.environ.get("COMFYUI_USER_DIR", "user")) / "runpod-agentic" / "state.sqlite"


def dependency_ready_timeout_seconds() -> int:
    return int(os.environ.get("RUNPOD_DEPENDENCY_READY_TIMEOUT_SECONDS", "1200"))


def agent_response_timeout_seconds() -> int:
    return int(os.environ.get("CRAG_AGENT_RESPONSE_TIMEOUT_SECONDS", "120"))


class ProgressReporter(Protocol):
    def set_total(self, total: int) -> None: ...
    def update(self, message: str = "") -> None: ...


@dataclass
class RunpodRunner:
    runpod_client: RunpodClientProtocol | None = None
    ssh_client: SSHClientProtocol | None = None
    state_store: StateStore | None = None
    planner: Planner | None = None
    progress: ProgressReporter | None = None

    def __post_init__(self) -> None:
        if self.runpod_client is None:
            self.runpod_client = RunpodClient()
        if self.ssh_client is None:
            self.ssh_client = SubprocessSSHClient()
        if self.state_store is None:
            self.state_store = StateStore(default_state_path())
        if self.planner is None:
            self.planner = Planner()

    def run(self, deployment: DeploymentSpec, *, mode: str, prompt: str = "", workflow_graph: Any = None, on_error: str = "stop_created") -> dict[str, Any]:
        validate_deployment(deployment, mode=mode, require_api_key=True)
        plan = self.planner.build(deployment, mode=mode, prompt=prompt, workflow_graph=workflow_graph)
        self._set_progress_total(plan)
        self.reconcile_managed_pods()
        self.state_store.record_run(plan.run_id, plan.workflow_hash, plan.deployment_hash, mode, "started")
        try:
            if mode in {"stop", "terminate", "destroy"}:
                return self._lifecycle(plan, mode)
            result = self._apply(plan, wait=mode == "apply_and_wait")
            self.state_store.record_run(plan.run_id, plan.workflow_hash, plan.deployment_hash, mode, "completed")
            self._progress_step("completed")
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
            self._progress_step(f"create {resource.role}")
            pending_resource_id = self.state_store.record_resource(plan.run_id, resource, status="creating")
            pod = self._reuse_or_create_pod(plan, resource, pending_resource_id)
            created[resource.name] = pod
            resource_id = self.state_store.record_resource(plan.run_id, resource, pod, status=pod.get("desiredStatus", "created"))
            resource_ids[resource.name] = resource_id
            if resource.role != "agent":
                self._progress_step(f"wait {resource.role}")
                created[resource.name] = self._wait_dependency_ready(resource, pod, plan.run_id, resource_id)

        agent = next(resource for resource in plan.resources if resource.role == "agent")
        resolved_contract = resolve_dependency_endpoints(plan, created)
        plan = replace(plan, runtime_contract=resolved_contract)
        agent_pod = created[agent.name]
        self._progress_step("wait ssh")
        host, port = self._wait_ssh_endpoint(agent_pod, plan)
        self._wait_ssh_ready(host, port)
        response, errors = self._run_agent_ssh_steps(plan, host, port, resource_ids.get(agent.name), wait=wait)
        keep_alive_result = self._enforce_response_keep_alive(plan, agent_pod, response_collected=bool(response))
        status = "waiting" if wait else "launched"
        if wait and response:
            status = "completed"
        result = {"run_id": plan.run_id, "status": status, "pods": {name: pod.get("id") for name, pod in created.items()}, "response": response, "errors": errors, "plan": plan.to_dict()}
        if keep_alive_result:
            result["keep_alive"] = keep_alive_result
        return result

    def _reuse_or_create_pod(self, plan: DeploymentPlan, resource, pending_resource_id: str) -> dict[str, Any]:
        existing = self._matching_existing_resource(plan, resource)
        if existing:
            pod_id = existing.get("runpod_pod_id")
            pod = self.runpod_client.get_pod(pod_id)
            status = str(pod.get("desiredStatus") or pod.get("status") or "").upper()
            if plan.reuse_policy == "reuse_matching" and status not in {"STOPPED", "EXITED", "TERMINATED"}:
                self.state_store.add_event(plan.run_id, "pod_reused", resource.name, resource_id=pending_resource_id, payload={"pod_id": pod_id})
                return pod
            if plan.reuse_policy == "resume_stopped" and status in {"STOPPED", "EXITED"}:
                resumed = self.runpod_client.resume_pod(pod_id)
                pod = {**pod, **resumed}
                self.state_store.add_event(plan.run_id, "pod_resumed", resource.name, resource_id=pending_resource_id, payload={"pod_id": pod_id})
                return pod
        self.state_store.add_event(
            plan.run_id,
            "pod_create_request",
            resource.name,
            resource_id=pending_resource_id,
            payload=sanitize_pod_input(resource.pod_input),
        )
        try:
            pod = self.runpod_client.create_or_deploy_pod(resource.pod_input)
        except Exception as exc:
            self.state_store.add_event(
                plan.run_id,
                "pod_create_failed",
                f"{resource.name}: {exc}",
                resource_id=pending_resource_id,
                payload={"role": resource.role, "template_id": resource.template_id, "input": sanitize_pod_input(resource.pod_input)},
            )
            raise
        self.state_store.add_event(plan.run_id, "pod_created", resource.name, resource_id=pending_resource_id, payload={"pod_id": pod.get("id")})
        return pod

    def _matching_existing_resource(self, plan: DeploymentPlan, resource) -> dict[str, Any] | None:
        if plan.reuse_policy == "always_create":
            return None
        for existing in self.state_store.list_resources():
            if existing.get("desired_hash") == resource.desired_hash and existing.get("runpod_pod_id"):
                return existing
        return None

    def _run_agent_ssh_steps(self, plan: DeploymentPlan, host: str, port: int, resource_id: str | None, *, wait: bool = False) -> tuple[str, str]:
        response_parts = []
        error_parts = []
        launched = False
        for action in plan.actions:
            if action.role != "agent":
                continue
            if action.action == "RUN_SSH_COMMAND":
                if action.detail.get("phase") == "teardown":
                    continue
                result = self._run_ssh_command_action(plan, action, host, port, resource_id)
                if result.stdout:
                    response_parts.append(result.stdout)
                if result.stderr:
                    error_parts.append(result.stderr)
            elif action.action == "WRITE_RUNTIME_CONFIG":
                self._progress_step("write runtime")
                self._write_runtime_files(plan, host, port)
            elif action.action == "LAUNCH_AGENT":
                launch = self._launch_command(plan)
                if not launch:
                    self.state_store.add_event(plan.run_id, "agent_launch_skipped", "manual startup mode")
                    continue
                self._progress_step("launch agent")
                result = self.ssh_client.run(host, port, launch)
                launched = True
                if result.stdout:
                    response_parts.append(result.stdout)
                if result.stderr:
                    error_parts.append(result.stderr)
                self.state_store.add_event(plan.run_id, "agent_launch", launch, payload={"exit_code": result.exit_code})
                if result.exit_code != 0:
                    raise RuntimeError(f"Agent launch failed with exit code {result.exit_code}.")
            elif action.action == "MONITOR_KEEP_ALIVE":
                continue
        if wait and launched:
            self._progress_step("wait response")
            response, errors = self._wait_agent_response(plan, host, port)
            if response:
                response_parts.append(response)
            if errors:
                error_parts.append(errors)
        return "".join(response_parts), "".join(error_parts)

    def _run_ssh_command_action(self, plan: DeploymentPlan, action, host: str, port: int, resource_id: str | None):
        self._progress_step(f"ssh {action.detail.get('phase', 'command')}")
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
        attempts = max(1, int(action.detail.get("retry_count") or 0) + 1)
        result = None
        for attempt in range(attempts):
            result = self.ssh_client.run(host, port, command)
            if result.exit_code == 0:
                break
            if action.detail.get("failure_policy") != "retry" or attempt >= attempts - 1:
                break
            self.state_store.add_event(plan.run_id, "ssh_command_retry", command, payload={"attempt": attempt + 1, "exit_code": result.exit_code})
            time.sleep(1)
        assert result is not None
        log_paths["stdout"].write_text(result.stdout)
        log_paths["stderr"].write_text(result.stderr)
        status = "completed" if result.exit_code == 0 else "failed"
        self.state_store.finish_command(command_id, status, result.exit_code)
        self.state_store.add_event(plan.run_id, "ssh_command", command, payload={"exit_code": result.exit_code})
        if result.exit_code != 0 and action.detail.get("failure_policy") in {"fail", "retry"}:
            raise RuntimeError(f"SSH command failed with exit code {result.exit_code}: {command}")
        return result

    def _wait_agent_response(self, plan: DeploymentPlan, host: str, port: int, timeout_seconds: int | None = None, interval_seconds: int = 2) -> tuple[str, str]:
        workspace = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"].get("WORKSPACE_DIR", "/workspace")
        base = workspace.rstrip("/") + "/.runpod_agentic"
        response_path = plan.runtime_contract.env.values.get("AGENT_RESPONSE_FILE", f"{base}/response.txt")
        errors_path = plan.runtime_contract.env.values.get("AGENT_ERRORS_FILE", f"{base}/errors.txt")
        log_path = f"{base}/agent.log"
        deadline = time.monotonic() + (timeout_seconds if timeout_seconds is not None else agent_response_timeout_seconds())
        last_error = ""
        while time.monotonic() < deadline:
            response = self._read_remote_file(host, port, response_path)
            errors = self._read_remote_file(host, port, errors_path)
            if response is not None:
                self.state_store.add_event(plan.run_id, "agent_response_collected", response_path)
                return response, errors or ""
            log_text = self._read_remote_file(host, port, log_path)
            if log_text and "[crag-agent] complete status=" in log_text:
                self.state_store.add_event(plan.run_id, "agent_log_collected", log_path)
                return log_text, errors or ""
            last_error = errors or last_error
            time.sleep(interval_seconds)
        self.state_store.add_event(plan.run_id, "agent_response_timeout", response_path, payload={"timeout_seconds": timeout_seconds if timeout_seconds is not None else agent_response_timeout_seconds()})
        return "", last_error

    def _read_remote_file(self, host: str, port: int, path: str) -> str | None:
        result = self.ssh_client.run(host, port, f"test -s {shell_env(path)} && cat {shell_env(path)}", timeout_seconds=20)
        if result.exit_code != 0:
            return None
        return result.stdout

    def _enforce_response_keep_alive(self, plan: DeploymentPlan, agent_pod: dict[str, Any], *, response_collected: bool) -> dict[str, Any] | None:
        policy = plan.keep_alive
        if not policy or policy.mode in {"manual", "time"} or policy.enforcement not in {"server_side", "both"}:
            return None
        pod_id = agent_pod.get("id")
        if not pod_id:
            return None
        if policy.mode == "turns":
            turns = self.state_store.increment_counter(plan.run_id, "turns") if response_collected else 0
            if policy.turn_limit and turns >= policy.turn_limit:
                self._apply_keep_alive_action(pod_id, policy)
                return {"mode": "turns", "action": policy.action, "turns": turns}
            return {"mode": "turns", "turns": turns}
        if policy.mode == "cost" and policy.cost_limit_usd:
            cost_per_hr = float(agent_pod.get("adjustedCostPerHr") or agent_pod.get("costPerHr") or 0)
            created = (agent_pod.get("runtime") or {}).get("uptimeInSeconds") or 0
            estimated = (float(created) / 3600.0) * cost_per_hr
            if estimated >= policy.cost_limit_usd:
                self._apply_keep_alive_action(pod_id, policy)
                return {"mode": "cost", "action": policy.action, "estimated_cost_usd": estimated}
            return {"mode": "cost", "estimated_cost_usd": estimated}
        return None

    def _apply_keep_alive_action(self, pod_id: str, policy: KeepAlivePolicy) -> None:
        self._progress_step(f"keep alive {policy.action}")
        if policy.action == "terminate":
            self.runpod_client.terminate_pod(pod_id)
        else:
            self.runpod_client.stop_pod(pod_id)

    def _set_progress_total(self, plan: DeploymentPlan) -> None:
        if not self.progress:
            return
        total = max(1, len(plan.resources) + len(plan.actions) + 2)
        self.progress.set_total(total)

    def _progress_step(self, message: str) -> None:
        if self.progress:
            self.progress.update(message)

    def _wait_dependency_ready(self, resource, pod: dict[str, Any], run_id: str, resource_id: str, timeout_seconds: int | None = None, interval_seconds: int = 5) -> dict[str, Any]:
        pod_id = pod.get("id")
        wants_public_endpoint = any(port.get("public") for port in resource.ports)
        timeout_seconds = timeout_seconds or dependency_ready_timeout_seconds()
        deadline = time.monotonic() + timeout_seconds
        last_status = pod.get("desiredStatus") or "unknown"
        while time.monotonic() < deadline:
            if wants_public_endpoint:
                endpoint = public_http_endpoint(pod)
                probe_path = first_ready_probe(endpoint, resource.role, resource.pod_input.get("env", {})) if endpoint else None
                if endpoint and probe_path:
                    self.state_store.add_event(run_id, "dependency_ready", resource.name, resource_id=resource_id, payload={"pod_id": pod_id, "endpoint": endpoint, "probe_path": probe_path})
                    return pod
            if not wants_public_endpoint and (pod.get("runtime") or {}).get("ports"):
                self.state_store.add_event(run_id, "dependency_ready", resource.name, resource_id=resource_id, payload={"pod_id": pod_id})
                return pod
            if pod_id:
                pod = self.runpod_client.get_pod(pod_id) or pod
                last_status = pod.get("desiredStatus") or last_status
            time.sleep(interval_seconds)
        raise RuntimeError(f"Timed out after {timeout_seconds}s waiting for dependency {resource.role} pod {pod_id} readiness; last status: {last_status}.")

    def _write_runtime_files(self, plan: DeploymentPlan, host: str, port: int) -> None:
        workspace = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"].get("WORKSPACE_DIR", "/workspace")
        base = workspace.rstrip("/") + "/.runpod_agentic"
        resources = {"resources": [to_plain(resource) for resource in plan.resources if resource.role != "agent"]}
        session_env = "\n".join(f"export {key}={shell_env(value)}" for key, value in sorted(plan.runtime_contract.env.values.items())) + "\n"
        commands = [action.detail for action in plan.actions if action.action == "RUN_SSH_COMMAND"]
        self.ssh_client.write_file(host, port, f"{base}/resources.json", json.dumps(resources, indent=2, sort_keys=True))
        self.ssh_client.write_file(host, port, f"{base}/session.env", session_env)
        self.ssh_client.write_file(host, port, f"{base}/commands.json", json.dumps(commands, indent=2, sort_keys=True))
        if plan.runtime_contract.env.values.get("AGENT_SYSTEM_PROMPT"):
            self.ssh_client.write_file(host, port, f"{base}/system_prompt.txt", plan.runtime_contract.env.values["AGENT_SYSTEM_PROMPT"])
        if plan.runtime_contract.env.values.get("AGENT_PROMPT"):
            self.ssh_client.write_file(host, port, f"{base}/prompt.txt", plan.runtime_contract.env.values["AGENT_PROMPT"])
        if plan.runtime_contract.env.values.get("MCP_SERVERS_JSON"):
            self.ssh_client.write_file(host, port, f"{base}/mcp_servers.json", plan.runtime_contract.env.values["MCP_SERVERS_JSON"])
        for relative_path, content in pi_runtime_files(plan.runtime_contract.env.values).items():
            self.ssh_client.write_file(host, port, f"{base}/{relative_path}", content)
        for relative_path, content in launcher_runtime_files().items():
            self.ssh_client.write_file(host, port, f"{base}/{relative_path}", content)
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
        return launch_command_for_plan(plan)

    def _lifecycle(self, plan: DeploymentPlan, mode: str) -> dict[str, Any]:
        resources = self.state_store.list_resources()
        matched = [resource for resource in resources if resource.get("desired_hash") in {item.desired_hash for item in plan.resources}]
        action = "terminate" if mode in {"terminate", "destroy"} else "stop"
        for resource in matched:
            pod_id = resource.get("runpod_pod_id")
            if not pod_id:
                continue
            self._run_teardown_commands(plan, resource)
            if action == "terminate":
                self.runpod_client.terminate_pod(pod_id)
            else:
                self.runpod_client.stop_pod(pod_id)
        return {"run_id": plan.run_id, "status": action, "resources": matched}

    def _run_teardown_commands(self, plan: DeploymentPlan, resource: dict[str, Any]) -> None:
        if resource.get("role") != "agent":
            return
        pod_id = resource.get("runpod_pod_id")
        if not pod_id:
            return
        teardown_actions = [action for action in plan.actions if action.action == "RUN_SSH_COMMAND" and action.detail.get("phase") == "teardown"]
        if not teardown_actions:
            return
        pod = self.runpod_client.get_pod(pod_id)
        host, port = self._wait_ssh_endpoint(pod, plan)
        self._wait_ssh_ready(host, port)
        for action in teardown_actions:
            self._run_ssh_command_action(plan, action, host, port, resource.get("id"))

    def _cleanup_created(self, plan: DeploymentPlan, *, terminate: bool) -> None:
        for resource in self.state_store.list_resources():
            if resource.get("run_id") != plan.run_id or not resource.get("runpod_pod_id"):
                continue
            if terminate:
                try:
                    self.runpod_client.terminate_pod(resource["runpod_pod_id"])
                except Exception as exc:
                    self.state_store.add_event(plan.run_id, "cleanup_terminate_failed", str(exc), resource_id=resource.get("id"))
            else:
                try:
                    self.runpod_client.stop_pod(resource["runpod_pod_id"])
                except Exception as exc:
                    self.state_store.add_event(plan.run_id, "cleanup_stop_failed", str(exc), resource_id=resource.get("id"))

    def reconcile_managed_pods(self, run_id: str | None = None) -> list[dict[str, Any]]:
        pods = [pod for pod in self.runpod_client.list_pods() if str(pod.get("name", "")).startswith("crag-")]
        for pod in pods:
            self.state_store.record_remote_resource(pod, run_id=run_id)
        return pods


def shell_env(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def launch_command_for_plan(plan: DeploymentPlan) -> str:
    agent_env = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"]
    workspace = agent_env.get("WORKSPACE_DIR", "/workspace")
    timer = keep_alive_pod_timer_command(plan)
    if agent_env.get("AGENT_STARTUP_MODE") == "manual":
        if not timer:
            return ""
        return " && ".join((f"cd {shell_env(workspace)}", "mkdir -p .runpod_agentic", timer))
    harness = agent_env.get("AGENT_HARNESS", "agent")
    configured_launcher = os.environ.get("CRAG_AGENT_LAUNCH_COMMAND") or agent_env.get("CRAG_AGENT_LAUNCH_COMMAND")
    if configured_launcher:
        launch = f"nohup bash -lc {shell_env(configured_launcher)} > .runpod_agentic/agent.log 2>&1 &"
    else:
        launch = (
            f"chmod +x .runpod_agentic/launcher.sh .runpod_agentic/launcher.d/*.sh .runpod_agentic/launcher.d/harnesses/*.sh 2>/dev/null || true && "
            f"nohup .runpod_agentic/launcher.sh {shell_env(harness)} > .runpod_agentic/agent.log 2>&1 &"
        )
    return " && ".join(
        part
        for part in (
            f"cd {shell_env(workspace)}",
            "mkdir -p .runpod_agentic",
            timer,
            launch,
        )
        if part
    )


def keep_alive_pod_timer_command(plan: DeploymentPlan) -> str:
    script = keep_alive_pod_timer_script(plan.keep_alive)
    if not script:
        return ""
    return f"mkdir -p .runpod_agentic && (nohup bash -lc {shell_env(script)} > .runpod_agentic/keepalive.log 2>&1 &)"


def keep_alive_pod_timer_script(policy: KeepAlivePolicy | None) -> str:
    if not policy or policy.mode != "time" or not policy.time_seconds or policy.enforcement not in {"pod_side", "both"}:
        return ""
    mutation = "podStop(input: $input) { id desiredStatus }" if policy.action == "stop" else "podTerminate(input: $input)"
    runpodctl_command = 'runpodctl stop pod "$RUNPOD_POD_ID"' if policy.action == "stop" else 'runpodctl remove pod "$RUNPOD_POD_ID" || runpodctl delete pod "$RUNPOD_POD_ID"'
    return f"""
set -euo pipefail
crag_dir="${{CRAG_RUNTIME_DIR:-${{WORKSPACE_DIR:-/workspace}}/.runpod_agentic}}"
mkdir -p "$crag_dir"
if [ -s "$crag_dir/keepalive.pid" ]; then
  old_pid="$(cat "$crag_dir/keepalive.pid" 2>/dev/null || true)"
  if [ -n "$old_pid" ]; then kill "$old_pid" 2>/dev/null || true; fi
fi
(
  sleep {int(policy.time_seconds)}
  echo "[crag-keepalive] time policy expired; action={policy.action}" >&2
  if [ -n "${{RUNPOD_POD_ID:-}}" ] && command -v runpodctl >/dev/null 2>&1; then
    {runpodctl_command} && exit 0
  fi
  if [ -n "${{RUNPOD_POD_ID:-}}" ] && [ -n "${{RUNPOD_API_KEY:-}}" ] && command -v curl >/dev/null 2>&1; then
    curl -fsS -X POST https://api.runpod.io/graphql \
      -H "Authorization: Bearer $RUNPOD_API_KEY" \
      -H "Content-Type: application/json" \
      --data "$(printf '{{"query":"mutation KeepAlive($input: Pod{"Stop" if policy.action == "stop" else "Terminate"}Input!) {{ {mutation} }}","variables":{{"input":{{"podId":"%s"}}}}}}' "$RUNPOD_POD_ID")" \
      && exit 0
  fi
  kill -TERM 1 2>/dev/null || true
) &
echo "$!" > "$crag_dir/keepalive.pid"
""".strip()


def startup_script_for_plan(plan: DeploymentPlan) -> str:
    agent_env = next(resource for resource in plan.resources if resource.role == "agent").pod_input["env"]
    workspace = agent_env.get("WORKSPACE_DIR", "/workspace")
    base = workspace.rstrip("/") + "/.runpod_agentic"
    commands = [action.detail for action in plan.actions if action.action == "RUN_SSH_COMMAND"]
    lines = [
        "bash <<'CRAG_STARTUP'",
        "set -euo pipefail",
        f"workspace={shell_env(workspace)}",
        "mkdir -p \"$workspace/.runpod_agentic\"",
        "cd \"$workspace\"",
        *file_write_lines(f"{base}/resources.json", json.dumps({"resources": [to_plain(resource) for resource in plan.resources if resource.role != "agent"]}, indent=2, sort_keys=True)),
        *file_write_lines(f"{base}/session.env", "\n".join(f"export {key}={shell_env(value)}" for key, value in sorted(plan.runtime_contract.env.values.items())) + "\n"),
        *file_write_lines(f"{base}/commands.json", json.dumps(commands, indent=2, sort_keys=True)),
    ]
    if plan.runtime_contract.env.values.get("AGENT_SYSTEM_PROMPT"):
        lines.extend(file_write_lines(f"{base}/system_prompt.txt", plan.runtime_contract.env.values["AGENT_SYSTEM_PROMPT"]))
    if plan.runtime_contract.env.values.get("AGENT_PROMPT"):
        lines.extend(file_write_lines(f"{base}/prompt.txt", plan.runtime_contract.env.values["AGENT_PROMPT"]))
    if plan.runtime_contract.env.values.get("MCP_SERVERS_JSON"):
        lines.extend(file_write_lines(f"{base}/mcp_servers.json", plan.runtime_contract.env.values["MCP_SERVERS_JSON"]))
    for relative_path, content in pi_runtime_files(plan.runtime_contract.env.values).items():
        lines.extend(file_write_lines(f"{base}/{relative_path}", content))
    for relative_path, content in launcher_runtime_files().items():
        lines.extend(file_write_lines(f"{base}/{relative_path}", content))
    for index, detail in enumerate(commands):
        if detail.get("phase") == "before_start":
            lines.extend(run_script_lines(f"crag_command_{index}", detail["command"]))
    launch = launch_command_for_plan(plan)
    if launch:
        lines.append(launch)
    else:
        lines.append("echo 'CRAG startup mode is manual; launcher not started.'")
    lines.append("CRAG_STARTUP")
    return "\n".join(lines)


def file_write_lines(path: str, content: str) -> list[str]:
    marker = "CRAG_FILE_" + str(abs(hash(path)))
    return [
        f"mkdir -p $(dirname {shell_env(path)})",
        f"cat > {shell_env(path)} <<'{marker}'",
        content,
        marker,
    ]


def run_script_lines(label: str, command: str) -> list[str]:
    marker = label.upper()
    return [
        f"bash <<'{marker}'",
        command,
        marker,
    ]


def launcher_runtime_files() -> dict[str, str]:
    return {
        "launcher.sh": agent_launcher_script(),
        "launcher.d/00-env.sh": launcher_env_script(),
        "launcher.d/10-preflight.sh": launcher_preflight_script(),
        "launcher.d/harnesses/codex.sh": codex_harness_script(),
        "launcher.d/harnesses/claude.sh": claude_harness_script(),
        "launcher.d/harnesses/hermes.sh": hermes_harness_script(),
        "launcher.d/harnesses/opencode.sh": opencode_harness_script(),
        "launcher.d/harnesses/pi.sh": pi_harness_script(),
        "launcher.d/harnesses/generic.sh": generic_harness_script(),
    }


def pi_runtime_files(env: dict[str, str]) -> dict[str, str]:
    if str(env.get("LLM_PROVIDER") or "").lower() != "ollama_cloud":
        return {}
    model = env.get("OLLAMA_MODEL") or env.get("LLM_MODEL") or "deepseek-v4-flash"
    base_url = (env.get("OLLAMA_HOST") or env.get("LLM_API_BASE_URL") or "https://ollama.com").rstrip("/")
    openai_base_url = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    models = {
        "providers": {
            "ollama-cloud": {
                "baseUrl": openai_base_url,
                "api": "openai-completions",
                "apiKey": "OLLAMA_CLOUD_API_KEY",
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [
                    {
                        "id": model,
                        "contextWindow": 128000,
                        "maxTokens": 32768,
                    }
                ],
            }
        }
    }
    providers = {
        "ollama-cloud": {
            "type": "api_key",
            "env": "OLLAMA_CLOUD_API_KEY",
            "baseUrl": openai_base_url,
        }
    }
    settings = {
        "defaultModel": model,
        "defaultProvider": "ollama-cloud",
        "provider": "ollama-cloud",
        "quietStartup": True,
    }
    return {
        "harness/pi/models.json": json.dumps(models, indent=2, sort_keys=True),
        "harness/pi/providers.json": json.dumps(providers, indent=2, sort_keys=True),
        "harness/pi/settings.json": json.dumps(settings, indent=2, sort_keys=True),
    }


def agent_launcher_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail

workspace="${WORKSPACE_DIR:-$(pwd)}"
crag_dir="${CRAG_RUNTIME_DIR:-$workspace/.runpod_agentic}"
harness="${1:-${AGENT_HARNESS:-agent}}"
launcher_dir="$crag_dir/launcher.d"
export WORKSPACE_DIR="$workspace"
export CRAG_RUNTIME_DIR="$crag_dir"
export AGENT_HARNESS="$harness"

run_hook_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    return 0
  fi
  local hook
  for hook in "$dir"/*.sh; do
    if [ -f "$hook" ]; then
      # shellcheck disable=SC1090
      . "$hook"
    fi
  done
}

cd "$workspace"
run_hook_dir "$launcher_dir"
run_hook_dir "$launcher_dir/pre.d"

if [ -n "${CRAG_AGENT_LAUNCH_COMMAND:-}" ]; then
  exec bash -lc "$CRAG_AGENT_LAUNCH_COMMAND"
fi

if command -v runpod-agent-launch >/dev/null 2>&1; then
  exec runpod-agent-launch "$harness"
fi

normalized_harness="$(printf '%s' "$harness" | tr '[:upper:]' '[:lower:]' | tr ' _' '--')"
harness_script="$launcher_dir/harnesses/$normalized_harness.sh"
if [ -f "$harness_script" ]; then
  exec bash "$harness_script"
fi

exec bash "$launcher_dir/harnesses/generic.sh"
"""


def launcher_env_script() -> str:
    return r"""#!/usr/bin/env bash
if [ -f "$CRAG_RUNTIME_DIR/session.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CRAG_RUNTIME_DIR/session.env"
  set +a
fi
export AGENT_MODEL="${AGENT_MODEL:-}"
export AGENT_PROMPT_FILE="${AGENT_PROMPT_FILE:-$CRAG_RUNTIME_DIR/prompt.txt}"
export AGENT_SYSTEM_PROMPT_FILE="${AGENT_SYSTEM_PROMPT_FILE:-$CRAG_RUNTIME_DIR/system_prompt.txt}"
export MCP_SERVERS_FILE="${MCP_SERVERS_FILE:-$CRAG_RUNTIME_DIR/mcp_servers.json}"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.cargo/bin:$PATH"
if [ -n "${OLLAMA_API_KEY:-}" ] && [ -z "${OLLAMA_CLOUD_API_KEY:-}" ]; then
  export OLLAMA_CLOUD_API_KEY="$OLLAMA_API_KEY"
fi
"""


def launcher_preflight_script() -> str:
    return r"""#!/usr/bin/env bash
if [ ! -d "$WORKSPACE_DIR" ]; then
  mkdir -p "$WORKSPACE_DIR"
fi
if [ -f "$MCP_SERVERS_FILE" ]; then
  export MCP_SERVERS_JSON="$(cat "$MCP_SERVERS_FILE")"
fi
if [ -d "$WORKSPACE_DIR/.codex/skills" ]; then
  mkdir -p "$HOME/.agents"
  ln -sfn "$WORKSPACE_DIR/.codex/skills" "$HOME/.agents/skills"
fi
if [ -d "$CRAG_RUNTIME_DIR/harness/pi" ]; then
  mkdir -p "$HOME/.pi/agent" "$WORKSPACE_DIR/.pi/agent"
  cp "$CRAG_RUNTIME_DIR/harness/pi/models.json" "$HOME/.pi/agent/models.json"
  cp "$CRAG_RUNTIME_DIR/harness/pi/providers.json" "$HOME/.pi/agent/providers.json"
  cp "$CRAG_RUNTIME_DIR/harness/pi/settings.json" "$HOME/.pi/agent/settings.json"
  ln -sfn "$HOME/.pi/agent/models.json" "$WORKSPACE_DIR/.pi/agent/models.json"
  ln -sfn "$HOME/.pi/agent/providers.json" "$WORKSPACE_DIR/.pi/agent/providers.json"
  ln -sfn "$HOME/.pi/agent/settings.json" "$WORKSPACE_DIR/.pi/agent/settings.json"
  export PI_MODELS_FILE="$HOME/.pi/agent/models.json"
  export PI_PROVIDERS_FILE="$HOME/.pi/agent/providers.json"
fi
"""


def codex_harness_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail
if ! command -v codex >/dev/null 2>&1; then
  exec bash "$CRAG_RUNTIME_DIR/launcher.d/harnesses/generic.sh"
fi
prompt=""
if [ -f "$AGENT_PROMPT_FILE" ]; then
  prompt="$(cat "$AGENT_PROMPT_FILE")"
fi
args=(exec)
if [ -n "$AGENT_MODEL" ]; then
  args+=(-m "$AGENT_MODEL")
fi
if [ -s "$AGENT_SYSTEM_PROMPT_FILE" ]; then
  args+=(--system-prompt "$(cat "$AGENT_SYSTEM_PROMPT_FILE")")
fi
exec codex "${args[@]}" "$prompt"
"""


def claude_harness_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail
if ! command -v claude >/dev/null 2>&1; then
  exec bash "$CRAG_RUNTIME_DIR/launcher.d/harnesses/generic.sh"
fi
prompt=""
if [ -f "$AGENT_PROMPT_FILE" ]; then
  prompt="$(cat "$AGENT_PROMPT_FILE")"
fi
args=(-p "$prompt")
if [ -n "$AGENT_MODEL" ]; then
  args+=(--model "$AGENT_MODEL")
fi
if [ -s "$AGENT_SYSTEM_PROMPT_FILE" ]; then
  args+=(--system-prompt "$(cat "$AGENT_SYSTEM_PROMPT_FILE")")
fi
exec claude "${args[@]}"
"""


def opencode_harness_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail
if ! command -v opencode >/dev/null 2>&1; then
  exec bash "$CRAG_RUNTIME_DIR/launcher.d/harnesses/generic.sh"
fi
prompt=""
if [ -f "$AGENT_PROMPT_FILE" ]; then
  prompt="$(cat "$AGENT_PROMPT_FILE")"
fi
args=(run)
if [ -n "$AGENT_MODEL" ]; then
  args+=(--model "$AGENT_MODEL")
fi
exec opencode "${args[@]}" "$prompt"
"""


def hermes_harness_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail
if ! command -v hermes >/dev/null 2>&1; then
  exec bash "$CRAG_RUNTIME_DIR/launcher.d/harnesses/generic.sh"
fi
prompt=""
if [ -f "$AGENT_PROMPT_FILE" ]; then
  prompt="$(cat "$AGENT_PROMPT_FILE")"
fi
args=(chat -q "$prompt")
if [ -n "$AGENT_MODEL" ]; then
  args+=(--model "$AGENT_MODEL")
fi
exec hermes "${args[@]}"
"""


def pi_harness_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail
if ! command -v pi >/dev/null 2>&1; then
  exec bash "$CRAG_RUNTIME_DIR/launcher.d/harnesses/generic.sh"
fi
mkdir -p "$CRAG_RUNTIME_DIR"
prompt=""
if [ -f "$AGENT_PROMPT_FILE" ]; then
  prompt="$(cat "$AGENT_PROMPT_FILE")"
fi
args=()
if [ -n "${AGENT_MODEL:-}" ]; then
  args+=(--model "$AGENT_MODEL")
fi
if [ "${LLM_PROVIDER:-}" = "ollama_cloud" ]; then
  args+=(--provider ollama-cloud)
elif [ -n "${PI_PROVIDER:-}" ]; then
  args+=(--provider "$PI_PROVIDER")
fi
response_file="${AGENT_RESPONSE_FILE:-$CRAG_RUNTIME_DIR/response.txt}"
errors_file="${AGENT_ERRORS_FILE:-$CRAG_RUNTIME_DIR/errors.txt}"
{
  echo "model: ${AGENT_MODEL:-}"
  echo "models_file: ${PI_MODELS_FILE:-$HOME/.pi/agent/models.json}"
  echo "providers_file: ${PI_PROVIDERS_FILE:-$HOME/.pi/agent/providers.json}"
  echo
  set +e
  pi "${args[@]}" -p "$prompt"
  status=$?
  set -e
  echo
  echo "[crag-agent] complete status=$status"
  exit "$status"
} > "$response_file" 2> "$errors_file"
cat "$response_file"
if [ -s "$errors_file" ]; then
  cat "$errors_file" >&2
fi
"""


def generic_harness_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<EOF
No compatible agent launcher was found for harness '${AGENT_HARNESS:-agent}'.
Install the requested agent CLI in the container, include runpod-agent-launch on PATH,
add a script at $CRAG_RUNTIME_DIR/launcher.d/harnesses/<harness>.sh,
or set CRAG_AGENT_LAUNCH_COMMAND to the exact startup command.
EOF
exit 127
"""


def stable_command_hash(command: str) -> str:
    import hashlib

    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def sanitize_pod_input(input: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(input, default=str))
    env = redacted.get("env")
    if isinstance(env, dict):
        for key in list(env):
            if is_sensitive_key(key):
                env[key] = "<redacted>"
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, dict) and is_sensitive_key(str(item.get("key", ""))):
                item["value"] = "<redacted>"
    return redacted


def is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))


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


def readiness_probe_paths(role: str, env: dict[str, Any]) -> list[str]:
    provider = str(env.get("LLM_PROVIDER") or env.get("VECTOR_PROVIDER") or "").lower()
    if role == "llm" and provider == "ollama":
        return ["/api/tags"]
    if role == "llm" and provider == "vllm":
        return ["/health", "/v1/models"]
    if role == "vector" and provider == "qdrant":
        return ["/readyz", "/collections"]
    if role == "vector" and provider == "chroma":
        return ["/api/v2/heartbeat"]
    return ["/"]


def first_ready_probe(endpoint: str, role: str, env: dict[str, Any], *, timeout_seconds: float = 3.0) -> str | None:
    for path in readiness_probe_paths(role, env):
        url = endpoint.rstrip("/") + path
        request = urllib.request.Request(url, headers={"Accept": "application/json,text/plain,*/*", "User-Agent": "comfyui-runpod-agentic/0.1"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if 200 <= response.status < 500:
                    return path
        except urllib.error.HTTPError as exc:
            if 200 <= exc.code < 500:
                return path
        except urllib.error.URLError:
            continue
    return None
