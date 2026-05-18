import json
from types import SimpleNamespace

import yaml

from comfyui_runpod_agentic.local_runtime import apply_compose_file, command_for_engine, compose_yaml_for_plan
from comfyui_runpod_agentic.nodes import (
    RunpodAgentNode,
    RunpodComposeYAMLNode,
    RunpodDockerComposeApplyNode,
    RunpodLLMServerNode,
    RunpodNetworkStorageNode,
    RunpodPodNode,
)
from comfyui_runpod_agentic.planner import Planner


def build_local_runtime_deployment(retention_policy="preserve"):
    storage = RunpodNetworkStorageNode().build("vol-workspace", "/workspace", retention_policy)[0]
    llm = RunpodLLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    return RunpodPodNode().build(agent, gpu_count=0, network_storage=storage)[0]


def test_compose_yaml_resolves_dependency_env_and_volumes():
    plan = Planner().build(build_local_runtime_deployment(), prompt="Use the local Ollama service.")

    compose = yaml.safe_load(compose_yaml_for_plan(plan, project_name="crag-test"))

    assert compose["name"] == "crag-test"
    assert any(service["image"].startswith("ollama/ollama") for service in compose["services"].values())
    agent = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")
    assert agent["environment"]["OLLAMA_HOST"].startswith("http://")
    assert agent["environment"]["OLLAMA_HOST"].endswith(":11434")
    assert agent["volumes"] == ["vol-workspace:/workspace"]
    assert compose["volumes"]["vol-workspace"]["labels"]["comfyui-runpod-agentic.retention_policy"] == "preserve"


def test_compose_yaml_preserves_volume_retention_intent():
    plan = Planner().build(build_local_runtime_deployment("delete_when_unused"))

    compose = yaml.safe_load(compose_yaml_for_plan(plan))

    assert compose["volumes"]["vol-workspace"]["labels"]["comfyui-runpod-agentic.retention_policy"] == "delete_when_unused"


def test_apply_compose_file_runs_docker_compose(monkeypatch, tmp_path):
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result = apply_compose_file("docker", compose_path, project_name="crag-test", action="up", timeout_seconds=7)

    assert result.returncode == 0
    assert calls[0][0] == ["docker", "compose", "-f", str(compose_path), "-p", "crag-test", "up", "-d"]
    assert calls[0][1]["timeout"] == 7


def test_containerd_uses_nerdctl_compose(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: "/usr/bin/nerdctl" if command == "nerdctl" else None)

    command = command_for_engine("containerd", "compose.yaml", "crag-test", "config")

    assert command == ["nerdctl", "compose", "-f", "compose.yaml", "-p", "crag-test", "config"]


def test_missing_containerd_runtime_returns_error_result(monkeypatch, tmp_path):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: None)
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")

    result = apply_compose_file("containerd", compose_path, project_name="crag-test", action="config")

    assert result.returncode == 127
    assert "nerdctl" in result.stderr


def test_compose_export_and_apply_nodes_save_files(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    export_path = tmp_path / "export.yaml"
    apply_path = tmp_path / "apply.yaml"

    compose_yaml, saved_path = RunpodComposeYAMLNode().export(deployment, project_name="crag-node", output_path=str(export_path))

    assert saved_path == str(export_path)
    assert "services:" in compose_yaml
    assert export_path.exists()

    monkeypatch.setattr(
        "comfyui_runpod_agentic.local_runtime.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="valid\n", stderr=""),
    )
    result_text, apply_yaml, apply_saved_path = RunpodDockerComposeApplyNode().apply(deployment, project_name="crag-node", output_path=str(apply_path), action="config")

    assert apply_saved_path == str(apply_path)
    assert apply_yaml == apply_path.read_text()
    assert json.loads(result_text)["command"] == ["docker", "compose", "-f", str(apply_path), "-p", "crag-node", "config"]
