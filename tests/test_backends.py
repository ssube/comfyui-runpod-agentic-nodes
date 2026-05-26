import json
import subprocess

from comfyui_runpod_agentic.backends import (
    ContainerBuildBackend,
    LocalContainerBackend,
    RunpodBackend,
    RuntimeOptions,
    RuntimeResult,
    commit_local_container_image,
)
from comfyui_runpod_agentic.local_runtime import LocalApplyResult
from comfyui_runpod_agentic.nodes import AgentNode, DeployNode, LanguageRuntimeNode


def test_runtime_result_formats_payload_for_node_outputs():
    result = RuntimeResult({"status": "completed", "response": "ok"}, response="ok", errors="")

    assert json.loads(result.json_text()) == {"status": "completed", "response": "ok"}


def test_runpod_backend_delegates_apply_to_runner(monkeypatch):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    seen = {}

    class FakeRunner:
        def __init__(self, progress=None):
            seen["progress"] = progress

        def run(self, deployment, *, mode, prompt, workflow_graph, on_error):
            seen["args"] = (mode, prompt, workflow_graph, on_error)
            return {"status": "completed", "response": "reply", "errors": ""}

    monkeypatch.setattr("comfyui_runpod_agentic.runner.RunpodRunner", FakeRunner)

    result = RunpodBackend(progress=object()).apply(deployment, RuntimeOptions(action="apply", prompt="hello", workflow_graph={"id": 1}, on_error="leave_running"))

    assert result.response == "reply"
    assert seen["args"] == ("apply", "hello", {"id": 1}, "leave_running")
    assert seen["progress"] is not None


def test_local_backend_preserves_apply_payload_and_artifacts(monkeypatch, tmp_path):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    output_path = tmp_path / "compose.yaml"
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.compose_yaml_for_plan", lambda *_args, **_kwargs: "services: {}\n")
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.write_compose_file", lambda *_args, **_kwargs: str(output_path))
    monkeypatch.setattr(
        "comfyui_runpod_agentic.local_runtime.apply_local_runtime_plan",
        lambda *_args, **_kwargs: (LocalApplyResult("docker", "apply", str(output_path), ["docker"], 0, "up\n", ""), True),
    )
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.enforce_local_keep_alive", lambda *_args, **_kwargs: None)

    result = LocalContainerBackend().apply(deployment, RuntimeOptions(action="apply", engine="docker", output_path=str(output_path), response_timeout_seconds=0))

    assert result.payload["reused"] is True
    assert result.artifacts == {"compose_yaml": "services: {}\n", "saved_path": str(output_path)}


def test_container_build_backend_commits_agent_container_from_host_runtime(monkeypatch, tmp_path):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    commands = LanguageRuntimeNode().build("nodejs", 22)[0]
    deployment = DeployNode().build(agent, commands=commands)[0]
    output_path = tmp_path / "build.yaml"
    seen = {}

    def fake_apply(engine, saved_path, project, plan, *, action, timeout_seconds):
        seen["engine"] = engine
        seen["commands"] = [item.detail["command"] for item in plan.actions if item.action == "RUN_SSH_COMMAND"]
        return LocalApplyResult(engine, action, saved_path, ["docker"], 0, "built\n", ""), False

    def fake_commit(engine, container_id, options):
        seen["commit"] = (engine, container_id, options.image_tag)
        return subprocess.CompletedProcess(["docker", "commit"], 0, "committed\n", "")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.compose_yaml_for_plan", lambda *_args, **_kwargs: "services: {}\n")
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.write_compose_file", lambda *_args, **_kwargs: str(output_path))
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.apply_local_runtime_plan", fake_apply)
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.find_local_runtime_container", lambda *_args, **_kwargs: "agent1")
    monkeypatch.setattr("comfyui_runpod_agentic.backends.commit_local_container_image", fake_commit)

    result = ContainerBuildBackend().apply(deployment, RuntimeOptions(image_tag="example/crag:latest", container_runtime="docker", output_path=str(output_path)))

    assert result.response == "built\n\ncommitted\n"
    assert seen["engine"] == "docker"
    assert seen["commit"] == ("docker", "agent1", "example/crag:latest")
    assert any("deb.nodesource.com/node_22.x" in command for command in seen["commands"])


def test_commit_local_container_image_commits_and_pushes_with_login(monkeypatch):
    calls = []

    monkeypatch.setenv("DOCKERHUB_USERNAME", "user")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "token")
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.local_runtime_command", lambda _engine, args: ["docker", *args])

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("input")))
        return subprocess.CompletedProcess(command, 0, f"{command[1]} ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = commit_local_container_image("docker", "agent1", RuntimeOptions(image_tag="example/crag:latest", container_runtime="docker", push_to_docker_hub=True))

    assert result.returncode == 0
    assert [call[0][1] for call in calls] == ["commit", "login", "push"]
    assert calls[1][1] == "token"
    assert "push ok" in result.stdout


def test_commit_local_container_image_returns_failed_command(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.local_runtime_command", lambda _engine, args: ["docker", *args])
    monkeypatch.setattr(subprocess, "run", lambda command, **_kwargs: subprocess.CompletedProcess(command, 2, "", "commit failed\n"))

    result = commit_local_container_image("docker", "agent1", RuntimeOptions(image_tag="example/crag:latest", container_runtime="docker"))

    assert result.returncode == 2
    assert result.args == ["docker", "commit", "agent1", "example/crag:latest"]
    assert "commit failed" in result.stderr
