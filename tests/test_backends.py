import json

from comfyui_runpod_agentic.backends import ContainerBuildBackend, LocalContainerBackend, RunpodBackend, RuntimeOptions, RuntimeResult
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


def test_container_build_backend_adds_snapshot_command_and_uses_local_apply(monkeypatch, tmp_path):
    agent = AgentNode().build("Pi", "model", "manual")[0]
    commands = LanguageRuntimeNode().build("nodejs", 22)[0]
    deployment = DeployNode().build(agent, commands=commands)[0]
    output_path = tmp_path / "build.yaml"
    seen = {}

    def fake_apply(engine, saved_path, project, plan, *, action, timeout_seconds):
        seen["engine"] = engine
        seen["commands"] = [item.detail["command"] for item in plan.actions if item.action == "RUN_SSH_COMMAND"]
        return LocalApplyResult(engine, action, saved_path, ["docker"], 0, "built\n", ""), False

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.compose_yaml_for_plan", lambda *_args, **_kwargs: "services: {}\n")
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.write_compose_file", lambda *_args, **_kwargs: str(output_path))
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.apply_local_runtime_plan", fake_apply)

    result = ContainerBuildBackend().apply(deployment, RuntimeOptions(image_tag="example/crag:latest", container_runtime="docker", output_path=str(output_path)))

    assert result.response == "built\n"
    assert seen["engine"] == "docker"
    assert any("\"$runtime\" commit \"$container_id\" \"$image_tag\"" in command for command in seen["commands"])
    assert any("image_tag=example/crag:latest" in command for command in seen["commands"])
    assert any("deb.nodesource.com/node_22.x" in command for command in seen["commands"])
