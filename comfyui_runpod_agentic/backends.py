from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .planner import DeploymentPlan, Planner
from .specs import DeploymentSpec


@dataclass(frozen=True)
class RuntimeOptions:
    action: str = "plan"
    prompt: str = ""
    workflow_graph: Any = None
    on_error: str = "stop_created"
    engine: str = "containerd"
    project_name: str = "crag-local"
    output_path: str = "artifacts/local-runtime/compose.yaml"
    use_sudo: bool = False
    timeout_seconds: int = 1800
    response_role: str = "agent"
    response_path: str = "/workspace/e2e/agent-skill-report.txt"
    response_image_path: str = ""
    response_timeout_seconds: int = 120
    image_tag: str = ""
    container_runtime: str = "nerdctl"
    push_to_docker_hub: bool = False
    dockerhub_username_env: str = "DOCKERHUB_USERNAME"
    dockerhub_token_env: str = "DOCKERHUB_TOKEN"
    failure_policy: str = "fail"
    retry_count: int = 0


@dataclass(frozen=True)
class RuntimeResult:
    payload: dict[str, Any]
    response: str = ""
    errors: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)

    def json_text(self) -> str:
        return json.dumps(self.payload, indent=2, sort_keys=True)


class RuntimeBackend(Protocol):
    def plan(self, deployment: DeploymentSpec, options: RuntimeOptions) -> DeploymentPlan: ...
    def apply(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult: ...
    def apply_and_wait(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult: ...
    def stop(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult: ...
    def terminate(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult: ...


class RunpodBackend:
    def __init__(self, *, progress: Any = None, planner: Planner | None = None):
        self.progress = progress
        self.planner = planner or Planner()

    def plan(self, deployment: DeploymentSpec, options: RuntimeOptions) -> DeploymentPlan:
        plan = self.planner.build(deployment, mode="plan", prompt=options.prompt, workflow_graph=options.workflow_graph)
        if self.progress:
            self.progress.set_total(max(1, len(plan.actions)))
            self.progress.update("plan")
        return plan

    def apply(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def apply_and_wait(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def stop(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def terminate(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def _run(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        from . import runner as runner_module

        try:
            try:
                runner = runner_module.RunpodRunner(progress=self.progress)
            except TypeError:
                runner = runner_module.RunpodRunner()
            try:
                payload = runner.run(deployment, mode=options.action, prompt=options.prompt, workflow_graph=options.workflow_graph, on_error=options.on_error, response_image_path=options.response_image_path)
            except TypeError as exc:
                if "response_image_path" not in str(exc):
                    raise
                payload = runner.run(deployment, mode=options.action, prompt=options.prompt, workflow_graph=options.workflow_graph, on_error=options.on_error)
        except Exception as exc:
            payload = {"status": "failed", "mode": options.action, "error": str(exc), "errors": str(exc)}
        return RuntimeResult(payload=payload, response=str(payload.get("response") or ""), errors=str(payload.get("errors") or ""), artifacts={"image_path": str(payload.get("image_path") or "")})


class LocalContainerBackend:
    def __init__(self, *, planner: Planner | None = None):
        self.planner = planner or Planner()

    def plan(self, deployment: DeploymentSpec, options: RuntimeOptions) -> DeploymentPlan:
        return self.planner.build(deployment, mode="plan", prompt=options.prompt, workflow_graph=options.workflow_graph)

    def apply(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def apply_and_wait(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def stop(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def terminate(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self._run(deployment, options)

    def _run(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        from . import local_runtime

        project = options.project_name.strip() or "crag-local"
        plan = self.plan(deployment, options)
        compose_yaml = local_runtime.compose_yaml_for_plan(plan, project_name=project)
        saved_path = local_runtime.write_compose_file(options.output_path, compose_yaml)
        old_sudo = os.environ.get("CRAG_LOCAL_RUNTIME_SUDO")
        if options.use_sudo:
            os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"
        else:
            os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
        try:
            result, reused = local_runtime.apply_local_runtime_plan(options.engine, saved_path, project, plan, action=options.action, timeout_seconds=int(options.timeout_seconds))
            response = ""
            response_errors = ""
            image_artifact_path = ""
            keep_alive_result = None
            if options.action in {"apply", "apply_and_wait"} and result.returncode == 0:
                keep_alive_result = local_runtime.enforce_local_keep_alive(options.engine, saved_path, project, plan, response_collected=False)
                if options.response_path.strip() and int(options.response_timeout_seconds) > 0:
                    read_result = local_runtime.read_local_runtime_file(options.engine, project, options.response_role.strip() or "agent", options.response_path.strip(), timeout_seconds=int(options.response_timeout_seconds))
                    response = read_result.stdout
                    response_errors = read_result.stderr
                    response_keep_alive_result = local_runtime.enforce_local_keep_alive(options.engine, saved_path, project, plan, response_collected=bool(response))
                    keep_alive_result = response_keep_alive_result or keep_alive_result
                if options.response_image_path.strip() and int(options.response_timeout_seconds) > 0:
                    image_read = local_runtime.copy_local_runtime_file(
                        options.engine,
                        project,
                        options.response_role.strip() or "agent",
                        options.response_image_path.strip(),
                        timeout_seconds=int(options.response_timeout_seconds),
                    )
                    if image_read.returncode == 0:
                        image_artifact_path = image_read.stdout.strip()
                    else:
                        response_errors = "\n".join(part for part in (response_errors, image_read.stderr) if part)
        finally:
            if old_sudo is None:
                os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
            else:
                os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = old_sudo
        payload = json.loads(result.to_text())
        payload["reused"] = reused
        terminal_urls = local_terminal_urls_for_plan(plan) if options.action in {"apply", "apply_and_wait"} and result.returncode == 0 else {}
        if terminal_urls:
            payload["terminal_urls"] = terminal_urls
            terminal_auth = local_terminal_auth_for_plan(plan)
            if terminal_auth:
                payload["terminal_auth"] = terminal_auth
        if keep_alive_result:
            payload["keep_alive"] = json.loads(keep_alive_result.to_text())
        errors = "\n".join(part for part in (result.stderr, response_errors, keep_alive_result.stderr if keep_alive_result else "") if part)
        return RuntimeResult(payload=payload, response=response, errors=errors, artifacts={"compose_yaml": compose_yaml, "saved_path": saved_path, "image_path": image_artifact_path})


class ContainerBuildBackend:
    def __init__(self, *, planner: Planner | None = None):
        self.planner = planner or Planner()

    def plan(self, deployment: DeploymentSpec, options: RuntimeOptions) -> DeploymentPlan:
        return self.planner.build(self._build_deployment(deployment, options), mode="plan", prompt=f"Build container {options.image_tag}", workflow_graph=options.workflow_graph)

    def apply(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        from . import local_runtime

        project = options.project_name.strip() or "crag-build"
        plan = self.plan(deployment, options)
        compose_yaml = local_runtime.compose_yaml_for_plan(plan, project_name=project)
        saved_path = local_runtime.write_compose_file(options.output_path, compose_yaml)
        old_sudo = os.environ.get("CRAG_LOCAL_RUNTIME_SUDO")
        if options.use_sudo:
            os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = "1"
        else:
            os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
        engine = {"nerdctl": "containerd", "docker": "docker", "podman": "podman"}[options.container_runtime]
        try:
            result, reused = local_runtime.apply_local_runtime_plan(engine, saved_path, project, plan, action="apply_and_wait", timeout_seconds=int(options.timeout_seconds))
            build_stdout = ""
            build_stderr = ""
            if result.returncode == 0:
                container_id = local_runtime.find_local_runtime_container(engine, project, "agent")
                if not container_id:
                    build_stderr = f"No running agent container found for build project {project}."
                    result_returncode = 1
                else:
                    build_result = commit_local_container_image(engine, container_id, options)
                    build_stdout = build_result.stdout
                    build_stderr = build_result.stderr
                    result_returncode = build_result.returncode
                if result_returncode != 0:
                    result = local_runtime.LocalApplyResult(
                        engine=result.engine,
                        action=result.action,
                        compose_path=result.compose_path,
                        command=result.command,
                        returncode=result_returncode,
                        stdout=result.stdout,
                        stderr="\n".join(part for part in (result.stderr, build_stderr) if part),
                    )
        finally:
            if old_sudo is None:
                os.environ.pop("CRAG_LOCAL_RUNTIME_SUDO", None)
            else:
                os.environ["CRAG_LOCAL_RUNTIME_SUDO"] = old_sudo
        payload = json.loads(result.to_text())
        payload["reused"] = reused
        payload["image_name"] = options.image_tag
        response = "\n".join(part for part in (result.stdout, build_stdout) if part)
        errors = "\n".join(part for part in (result.stderr, build_stderr) if part)
        return RuntimeResult(payload=payload, response=response, errors=errors, artifacts={"compose_yaml": compose_yaml, "saved_path": saved_path, "image_name": options.image_tag})

    def apply_and_wait(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        return self.apply(deployment, options)

    def stop(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        raise NotImplementedError("Container builds only support apply.")

    def terminate(self, deployment: DeploymentSpec, options: RuntimeOptions) -> RuntimeResult:
        raise NotImplementedError("Container builds only support apply.")

    def _build_deployment(self, deployment: DeploymentSpec, options: RuntimeOptions) -> DeploymentSpec:
        return replace(deployment, reuse_policy="always_create")


def commit_local_container_image(engine: str, container_id: str, options: RuntimeOptions) -> subprocess.CompletedProcess[str]:
    from . import local_runtime

    commands = [local_runtime.local_runtime_command(engine, ["commit", container_id, options.image_tag])]
    if options.push_to_docker_hub:
        username = os.environ.get(options.dockerhub_username_env.strip() or "DOCKERHUB_USERNAME", "")
        token = os.environ.get(options.dockerhub_token_env.strip() or "DOCKERHUB_TOKEN", "")
        if username and token:
            commands.append(local_runtime.local_runtime_command(engine, ["login", "docker.io", "-u", username, "--password-stdin"]))
        commands.append(local_runtime.local_runtime_command(engine, ["push", options.image_tag]))

    stdout: list[str] = []
    stderr: list[str] = []
    for command in commands:
        input_text = None
        if "login" in command and "--password-stdin" in command:
            input_text = os.environ.get(options.dockerhub_token_env.strip() or "DOCKERHUB_TOKEN", "")
        completed = subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=int(options.timeout_seconds), check=False)
        stdout.append(completed.stdout)
        stderr.append(completed.stderr)
        if completed.returncode != 0:
            return subprocess.CompletedProcess(command, completed.returncode, "\n".join(stdout), "\n".join(stderr))
    return subprocess.CompletedProcess(commands[-1] if commands else [], 0, "\n".join(stdout), "\n".join(stderr))


def runtime_node_output(result: RuntimeResult, names: tuple[str, ...], extra: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    values = (result.json_text(), result.response, result.errors, *extra)
    return values[: len(names)]


def with_reuse_policy(deployment: DeploymentSpec, reuse_policy: str) -> DeploymentSpec:
    return replace(deployment, reuse_policy=reuse_policy)


def local_terminal_urls_for_plan(plan: DeploymentPlan) -> dict[str, str]:
    urls = {}
    for resource in plan.resources:
        env = resource.pod_input.get("env") or {}
        if env.get("CRAG_WEB_TERMINAL") != "1":
            continue
        host_port = int(env.get("CRAG_WEB_TERMINAL_HOST_PORT") or env.get("CRAG_WEB_TERMINAL_PORT") or 7681)
        if host_port > 0:
            urls[resource.role] = f"http://127.0.0.1:{host_port}"
    return urls


def local_terminal_auth_for_plan(plan: DeploymentPlan) -> dict[str, dict[str, str]]:
    auth = {}
    for resource in plan.resources:
        env = resource.pod_input.get("env") or {}
        if env.get("CRAG_WEB_TERMINAL") != "1" or env.get("CRAG_WEB_TERMINAL_AUTH_MODE") != "password":
            continue
        username = str(env.get("CRAG_WEB_TERMINAL_USERNAME") or "")
        password = str(env.get("CRAG_WEB_TERMINAL_PASSWORD") or "")
        if username and password:
            auth[resource.role] = {"username": username, "password": password}
    return auth
