import json
import os
from types import SimpleNamespace

import yaml

from comfyui_runpod_agentic.local_runtime import (
    apply_compose_file,
    apply_local_runtime_plan,
    command_for_engine,
    compose_yaml_for_plan,
    enforce_local_keep_alive,
    local_runtime_response_is_ready,
    resolve_local_secret_placeholder,
)
from comfyui_runpod_agentic.nodes import (
    RunpodAgentNode,
    RunpodComposeYAMLNode,
    RunpodDockerComposeApplyNode,
    RunpodKeepAliveNode,
    RunpodLLMServerNode,
    RunpodNetworkStorageNode,
    RunpodPodNode,
    RunpodSSHCommandNode,
)
from comfyui_runpod_agentic.planner import Planner


def build_local_runtime_deployment(retention_policy="preserve"):
    storage = RunpodNetworkStorageNode().build("vol-workspace", "/workspace", retention_policy)[0]
    llm = RunpodLLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    return RunpodPodNode().build(agent, gpu_count=0, network_storage=storage)[0]


def build_reusable_local_runtime_deployment():
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace")[0]
    keep_alive = RunpodKeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0)[0]
    return RunpodPodNode().build(agent, gpu_count=0, keep_alive=keep_alive, reuse_policy="reuse_matching")[0]


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


def test_local_runtime_resolves_ollama_env_file_secret(tmp_path, monkeypatch):
    env_dir = tmp_path / ".env.d"
    env_dir.mkdir()
    ollama_env = env_dir / "ollama.env"
    ollama_env.write_text("export OLLAMA_API_KEY='ollama-test-key'\n")
    monkeypatch.setenv("OLLAMA_ENV_FILE", str(ollama_env))
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    value = resolve_local_secret_placeholder("{{ RUNPOD_SECRET_OLLAMA_API_KEY }}")

    assert value == "ollama-test-key"


def test_crag_agent_response_waits_for_completion_marker():
    assert not local_runtime_response_is_ready("/workspace/.runpod_agentic/response.txt", "model: deepseek-v4-flash\n")
    assert local_runtime_response_is_ready("/workspace/.runpod_agentic/response.txt", "answer\n[crag-agent] complete status=0\n")
    assert local_runtime_response_is_ready("/workspace/e2e/command-2.txt", "startup-two\n")


def test_agent_compose_command_runs_startup_commands():
    llm = RunpodLLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    commands = RunpodSSHCommandNode().build("printf startup-ok > /workspace/startup.txt", "before_start", 5, "fail")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0, commands=commands)[0]
    plan = Planner().build(deployment, prompt="List installed skills.")

    compose = yaml.safe_load(compose_yaml_for_plan(plan))
    agent_service = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    assert "run_crag_command" in agent_service["command"]
    assert ".runpod_agentic/prompt.txt" in agent_service["command"]
    assert ".runpod_agentic/launcher.sh" in agent_service["command"]
    assert "printf startup-ok > /workspace/startup.txt" in agent_service["command"]
    assert "$${label}" in agent_service["command"]
    assert "sleep infinity" in agent_service["command"]


def test_agent_compose_command_layers_pod_side_keep_alive():
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace")[0]
    keep_alive = RunpodKeepAliveNode().build("time", "terminate", 30, "seconds", 0, 0.0, 0, "pod_side")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0, keep_alive=keep_alive)[0]
    plan = Planner().build(deployment, prompt="wait")

    compose = yaml.safe_load(compose_yaml_for_plan(plan))
    agent_service = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    assert "runpodctl remove pod" in agent_service["command"]
    assert "RUNPOD_API_KEY" in agent_service["command"]
    assert "podTerminate" in agent_service["command"]
    assert "kill -TERM 1" in agent_service["command"]


def test_apply_compose_file_runs_docker_compose(monkeypatch, tmp_path):
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result = apply_compose_file("docker", compose_path, project_name="crag-test", action="apply", timeout_seconds=7)

    assert result.returncode == 0
    assert calls[0][0] == ["docker", "compose", "-f", str(compose_path), "-p", "crag-test", "up", "-d"]
    assert calls[0][1]["timeout"] == 7


def test_apply_local_runtime_reuses_matching_agent_container(monkeypatch, tmp_path):
    llm = RunpodLLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent_spec = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    keep_alive = RunpodKeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0)[0]
    deployment = RunpodPodNode().build(agent_spec, gpu_count=0, keep_alive=keep_alive, reuse_policy="reuse_matching")[0]
    plan = Planner().build(deployment, prompt="second prompt")
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    llm_resource = next(resource for resource in plan.resources if resource.role == "llm")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"llm1","Names":"crag-node-llm"}\n{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "llm1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "llm", "comfyui-runpod-agentic.desired_hash": llm_resource.desired_hash}}}]), stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": agent.desired_hash}}}]), stderr="")
        if command[:4] == ["docker", "exec", "agent1", "bash"]:
            return SimpleNamespace(returncode=0, stdout="relaunched\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is True
    assert result.action == "reuse"
    assert result.stdout == "relaunched\n"
    assert not any(command[:3] == ["docker", "compose", "-f"] for command in calls)
    assert any(command[:4] == ["docker", "exec", "agent1", "bash"] and "second prompt" in command[-1] for command in calls)


def test_apply_local_runtime_recreates_when_dependency_container_is_missing(monkeypatch, tmp_path):
    llm = RunpodLLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent_spec = RunpodAgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    deployment = RunpodPodNode().build(agent_spec, gpu_count=0, reuse_policy="reuse_matching")[0]
    plan = Planner().build(deployment, prompt="second prompt")
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": agent.desired_hash}}}]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    _result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is False
    assert ["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "up", "-d"] in calls
    assert not any(command[:4] == ["docker", "exec", "agent1", "bash"] for command in calls)


def test_apply_local_runtime_creates_when_reuse_is_disabled(monkeypatch, tmp_path):
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0, reuse_policy="always_create")[0]
    plan = Planner().build(deployment, prompt="first prompt")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    _result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is False
    assert calls[0][:5] == ["docker", "compose", "-f", str(compose_path), "-p"]


def test_enforce_local_keep_alive_turn_limit_stops_after_response(monkeypatch, tmp_path):
    deployment = build_reusable_local_runtime_deployment()
    plan = Planner().build(deployment, prompt="one turn")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="stopped\n", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result = enforce_local_keep_alive("docker", compose_path, "crag-node", plan, response_collected=True)

    assert result is not None
    assert result.action == "stop"
    assert calls[0] == ["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "stop"]


def test_enforce_local_keep_alive_skips_server_timer_for_pod_side(monkeypatch, tmp_path):
    agent = RunpodAgentNode().build("Pi", "model", "manual", "/workspace")[0]
    keep_alive = RunpodKeepAliveNode().build("time", "stop", 30, "seconds", 0, 0.0, 0, "pod_side")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0, keep_alive=keep_alive)[0]
    plan = Planner().build(deployment, prompt="one turn")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.Popen", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(pid=123))

    result = enforce_local_keep_alive("docker", compose_path, "crag-node", plan, response_collected=False)

    assert result is None
    assert calls == []


def test_containerd_uses_nerdctl_compose(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: "/usr/bin/nerdctl" if command == "nerdctl" else None)
    monkeypatch.delenv("CRAG_LOCAL_RUNTIME_SUDO", raising=False)

    command = command_for_engine("containerd", "compose.yaml", "crag-test", "config")

    assert command == ["nerdctl", "compose", "-f", "compose.yaml", "-p", "crag-test", "config"]


def test_local_runtime_can_prefix_any_engine_with_sudo(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: "/usr/bin/nerdctl" if command == "nerdctl" else None)
    monkeypatch.setenv("CRAG_LOCAL_RUNTIME_SUDO", "1")

    docker_command = command_for_engine("docker", "compose.yaml", "crag-test", "config")
    containerd_command = command_for_engine("containerd", "compose.yaml", "crag-test", "config")

    assert docker_command == ["sudo", "docker", "compose", "-f", "compose.yaml", "-p", "crag-test", "config"]
    assert containerd_command == ["sudo", "nerdctl", "compose", "-f", "compose.yaml", "-p", "crag-test", "config"]


def test_apply_node_can_request_sudo(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    apply_path = tmp_path / "sudo.yaml"
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="valid\n", stderr="")

    monkeypatch.delenv("CRAG_LOCAL_RUNTIME_SUDO", raising=False)
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    RunpodDockerComposeApplyNode().apply(deployment, project_name="crag-node", output_path=str(apply_path), action="config", use_sudo=True)

    assert seen["command"] == ["sudo", "docker", "compose", "-f", str(apply_path), "-p", "crag-node", "config"]
    assert "CRAG_LOCAL_RUNTIME_SUDO" not in os.environ


def test_apply_node_reads_response_file_after_up(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    apply_path = tmp_path / "up.yaml"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout='[{"Config":{"Labels":{"comfyui-runpod-agentic.role":"agent"}}}]', stderr="")
        if command == ["docker", "exec", "agent1", "cat", "/workspace/result.txt"]:
            return SimpleNamespace(returncode=0, stdout="agent response\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result_text, response, errors, _compose_yaml, _saved_path = RunpodDockerComposeApplyNode().apply(
        deployment,
        project_name="crag-node",
        output_path=str(apply_path),
        action="apply",
        response_path="/workspace/result.txt",
    )

    assert json.loads(result_text)["returncode"] == 0
    assert response == "agent response\n"
    assert errors == ""
    assert ["docker", "exec", "agent1", "cat", "/workspace/result.txt"] in calls


def test_apply_node_falls_back_to_completed_container_logs(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    apply_path = tmp_path / "up.yaml"

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout='[{"Config":{"Labels":{"comfyui-runpod-agentic.role":"agent"}}}]', stderr="")
        if command == ["docker", "exec", "agent1", "cat", "/workspace/missing.txt"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="cat: missing\n")
        if command == ["docker", "logs", "agent1"]:
            return SimpleNamespace(returncode=0, stdout="[crag-local-runtime] startup mode is manual; launcher not started.\nscript response\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    _result_text, response, errors, _compose_yaml, _saved_path = RunpodDockerComposeApplyNode().apply(
        deployment,
        project_name="crag-node",
        output_path=str(apply_path),
        action="apply",
        response_path="/workspace/missing.txt",
        response_timeout_seconds=1,
    )

    assert "script response" in response
    assert errors == ""


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
    result_text, response, errors, apply_yaml, apply_saved_path = RunpodDockerComposeApplyNode().apply(deployment, project_name="crag-node", output_path=str(apply_path), action="config")

    assert apply_saved_path == str(apply_path)
    assert response == ""
    assert errors == ""
    assert apply_yaml == apply_path.read_text()
    assert json.loads(result_text)["command"] == ["docker", "compose", "-f", str(apply_path), "-p", "crag-node", "config"]
