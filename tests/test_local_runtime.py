import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from comfyui_runpod_agentic.local_runtime import (
    agent_run_script,
    apply_compose_file,
    apply_local_runtime_plan,
    command_for_engine,
    compose_yaml_for_plan,
    enforce_local_keep_alive,
    find_local_runtime_container,
    find_local_runtime_project_container,
    image_for_resource,
    inspect_container_labels,
    inspect_container_role,
    list_local_runtime_project_containers,
    local_port_mappings,
    local_resource_desired_hash,
    local_runtime_command,
    local_runtime_file_contents,
    local_runtime_file_writes,
    local_runtime_response_is_ready,
    parse_container_list,
    read_local_runtime_file,
    resolve_local_secret_placeholder,
    resource_as_runtime_json,
    schedule_local_lifecycle,
    stop_local_runtime_project_containers,
    volume_mount_for_resource,
)
from comfyui_runpod_agentic.nodes import (
    AgentNode,
    ComposeYAMLNode,
    DeployNode,
    KeepAliveNode,
    LanguageRuntimeNode,
    LLMApiNode,
    LLMServerNode,
    LocalSQLDatabaseNode,
    MCPServerNode,
    NetworkStorageNode,
    RemoteSQLDatabaseNode,
    RunLocalContainersNode,
    SSHCommandNode,
    VectorDatabaseNode,
    WebTerminalNode,
    populate_local_volume_ids,
)
from comfyui_runpod_agentic.planner import Planner


@pytest.fixture(autouse=True)
def isolate_local_runtime_sudo(monkeypatch):
    monkeypatch.delenv("CRAG_LOCAL_RUNTIME_SUDO", raising=False)


def build_local_runtime_deployment(retention_policy="preserve"):
    storage = NetworkStorageNode().build("vol-workspace", "/workspace", retention_policy)[0]
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    return DeployNode().build(agent, network_storage=storage)[0]


def build_reusable_local_runtime_deployment():
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    keep_alive = KeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0)[0]
    return replace(DeployNode().build(agent, keep_alive=keep_alive)[0], reuse_policy="reuse_matching")


def test_compose_yaml_resolves_dependency_env_and_volumes():
    plan = Planner().build(build_local_runtime_deployment(), prompt="Use the local Ollama service.")

    compose = yaml.safe_load(compose_yaml_for_plan(plan, project_name="crag-test"))

    assert compose["name"] == "crag-test"
    assert any(service["image"].startswith("ollama/ollama") for service in compose["services"].values())
    agent = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")
    assert agent["environment"]["OLLAMA_HOST"].startswith("http://")
    assert agent["environment"]["OLLAMA_HOST"].endswith(":11434")
    assert "vol-workspace:/workspace" in agent["volumes"]
    assert any(volume.endswith(":/workspace/.runpod_agentic") for volume in agent["volumes"])
    assert compose["volumes"]["vol-workspace"]["labels"]["comfyui-runpod-agentic.retention_policy"] == "preserve"


def test_compose_yaml_resolves_database_host_placeholders():
    sql = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", sql_database=sql)[0]
    plan = Planner().build(DeployNode().build(agent)[0])

    compose = yaml.safe_load(compose_yaml_for_plan(plan, project_name="crag-db"))
    agent_env = next(service["environment"] for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    assert agent_env["DATABASE_HOST"].startswith("crag-")
    assert agent_env["DATABASE_URL"].startswith("postgresql://app:app@crag-")
    assert ":5432/app" in agent_env["DATABASE_URL"]


def test_local_runtime_session_env_resolves_database_host_placeholders():
    sql = RemoteSQLDatabaseNode().build("Postgres", "own_pod", "app", "app")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", sql_database=sql)[0]
    plan = Planner().build(DeployNode().build(agent)[0])
    writes = "\n".join(local_runtime_file_writes(plan))

    assert "DATABASE_HOST=crag-" in writes
    assert "DATABASE_URL=postgresql://app:app@crag-" in writes
    assert "crag://sql/postgres" not in writes


def test_same_pod_llm_and_embedded_chroma_stay_in_agent_service():
    llm = LLMServerNode().build("Ollama", "llama3.2", "same_pod", "none")[0]
    vector = VectorDatabaseNode().build("Chroma", "embedded", "docs", "/workspace/vector")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", llm=llm, vector_database=vector)[0]
    plan = Planner().build(DeployNode().build(agent)[0])

    compose = yaml.safe_load(compose_yaml_for_plan(plan, project_name="crag-same-pod"))
    services = list(compose["services"].values())
    agent_env = services[0]["environment"]

    assert len(services) == 1
    assert agent_env["OLLAMA_HOST"] == "http://127.0.0.1:11434"
    assert agent_env["VECTOR_MODE"] == "embedded"
    assert "crag-database/SKILL.md" in "\n".join(local_runtime_file_writes(plan))


def test_compose_yaml_preserves_volume_retention_intent():
    plan = Planner().build(build_local_runtime_deployment("delete_when_unused"))

    compose = yaml.safe_load(compose_yaml_for_plan(plan))

    assert compose["volumes"]["vol-workspace"]["labels"]["comfyui-runpod-agentic.retention_policy"] == "delete_when_unused"


def test_local_runtime_resource_helpers_cover_roles_and_ports():
    plan = Planner().build(build_local_runtime_deployment("delete_when_unused"))
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    llm = next(resource for resource in plan.resources if resource.role == "llm")
    storage = volume_mount_for_resource(agent)

    assert image_for_resource(agent) == "ubuntu:24.04"
    assert image_for_resource(llm).startswith("ollama/ollama")
    assert storage == ("vol-workspace", "/workspace", "delete_when_unused")
    assert local_port_mappings(agent) == []


def test_local_runtime_uses_agent_image_name_override():
    agent = AgentNode().build("Pi", "model", "manual", image_name="docker.io/example/crag:latest")[0]
    plan = Planner().build(DeployNode().build(agent)[0])
    agent_resource = next(resource for resource in plan.resources if resource.role == "agent")
    compose = yaml.safe_load(compose_yaml_for_plan(plan))
    agent_service = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    assert image_for_resource(agent_resource) == "docker.io/example/crag:latest"
    assert agent_service["image"] == "docker.io/example/crag:latest"


def test_database_skill_files_are_written_by_local_runtime():
    sql = LocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", sql_database=sql)[0]
    plan = Planner().build(DeployNode().build(agent)[0])

    writes = "\n".join(local_runtime_file_writes(plan))

    assert "/workspace/.runpod_agentic/skills/crag-database/SKILL.md" in writes
    assert "name: crag-database" in writes


def test_local_port_mappings_skip_unpublished_or_dynamic_ports():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 0, "none", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", terminal=terminal)[0]
    plan = Planner().build(DeployNode().build(agent)[0])
    resource = next(resource for resource in plan.resources if resource.role == "agent")

    assert local_port_mappings(resource) == []


def test_local_runtime_parses_container_lists_and_labels(monkeypatch):
    assert parse_container_list("") == []
    assert parse_container_list('[{"ID":"one"}]') == [{"ID": "one"}]
    assert parse_container_list('{"ID":"one"}\n{"ID":"two"}\n') == [{"ID": "one"}, {"ID": "two"}]

    def fake_run(command, **kwargs):
        if command == ["docker", "inspect", "one"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"role": "agent"}}}]), stderr="")
        if command == ["docker", "inspect", "bad"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    assert inspect_container_labels("docker", "one") == {"role": "agent"}
    assert inspect_container_labels("docker", "bad") == {}
    assert inspect_container_role("docker", "one") is None


def test_local_runtime_container_queries_handle_empty_and_matching(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["docker", "ps", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n{"ID":"other","Names":"other-agent"}\n', stderr="")
        if command == ["docker", "ps", "-a", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": "hash1"}}}]), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    assert find_local_runtime_container("docker", "crag-node", "agent", "hash1") == "agent1"
    assert find_local_runtime_container("docker", "crag-node", "agent", "missing") is None
    assert find_local_runtime_project_container("docker", "crag-node", "agent", "hash1") == "agent1"


def test_local_runtime_container_queries_return_empty_on_ps_failure(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="failed"))

    assert list_local_runtime_project_containers("docker", "crag-node") == []
    assert find_local_runtime_container("docker", "crag-node", "agent") is None


def test_compose_yaml_publishes_web_terminal_only_for_agent():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", terminal=terminal)[0]
    deployment = DeployNode().build(agent)[0]
    plan = Planner().build(deployment)

    compose = yaml.safe_load(compose_yaml_for_plan(plan, project_name="crag-terminal"))
    agent_service = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    assert agent_service["ports"] == ["127.0.0.1:8765:7681"]
    assert "ttyd" in "\n".join(local_runtime_file_contents(plan).values())


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
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    commands = SSHCommandNode().build("printf startup-ok > /workspace/startup.txt", "before_start", "fail")[0]
    deployment = DeployNode().build(agent, commands=commands)[0]
    plan = Planner().build(deployment, prompt="List installed skills.")

    compose = yaml.safe_load(compose_yaml_for_plan(plan))
    agent_service = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    runtime_files = local_runtime_file_contents(plan)

    assert agent_service["command"] == "bash -lc 'bash /workspace/.runpod_agentic/local-runtime/run-agent.sh'"
    assert len(agent_service["command"]) < 100
    assert any(volume.endswith(":/workspace/.runpod_agentic") for volume in agent_service["volumes"])
    assert any("/tmp/crag-local-runtime/" in volume for volume in agent_service["volumes"])
    assert "run_crag_command" in runtime_files["local-runtime/run-agent.sh"]
    assert any("printf startup-ok > /workspace/startup.txt" in text for path, text in runtime_files.items() if path.startswith("local-runtime/commands/"))
    assert "printf startup-ok > /workspace/startup.txt" in "\n".join(runtime_files.values())
    assert "${label}" in runtime_files["local-runtime/run-agent.sh"]
    assert "sleep infinity" in runtime_files["local-runtime/run-agent.sh"]


def test_local_runtime_file_writes_include_prompt_system_mcp_and_pi_config():
    llm = LLMApiNode().build("Ollama Cloud", "deepseek-v4-flash", "OLLAMA_API_KEY")[0]
    mcp = MCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    agent = AgentNode().build("Pi", "deepseek-v4-flash", "manual", "/workspace", system_prompt="Be concise.", llm=llm, mcp_servers=mcp)[0]
    plan = Planner().build(DeployNode().build(agent)[0], prompt="Say hi.")

    script = "\n".join(local_runtime_file_writes(plan))
    resource_json = resource_as_runtime_json(next(resource for resource in plan.resources if resource.role == "agent"))

    assert "system_prompt.txt" in script
    assert "prompt.txt" in script
    assert "mcp_servers.json" in script
    assert "harness/pi/models.json" in script
    assert resource_json["role"] == "agent"
    assert resource_json["materialization"] == "own_pod"


def test_agent_compose_command_runs_after_commands_after_launch():
    agent = AgentNode().build("Pi", "model", "auto_start", "/workspace")[0]
    before = SSHCommandNode().build("printf before", "before_start", "fail")[0]
    after = SSHCommandNode().build("printf after", "after_start", "fail", previous=before)[0]
    deployment = DeployNode().build(agent, commands=after)[0]
    plan = Planner().build(deployment, prompt="run")

    script = agent_run_script(plan, keep_container_alive=True)

    launch_index = script.rindex("nohup .runpod_agentic/launcher.sh")
    assert script.rindex("run_crag_command before_start") < launch_index
    assert launch_index < script.rindex("run_crag_command after_start")


def test_terminal_compose_command_does_not_block_startup_commands():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "none", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", "/workspace", terminal=terminal)[0]
    commands = SSHCommandNode().build("printf startup-ok > /workspace/startup.txt", "before_start", "fail")[0]
    deployment = DeployNode().build(agent, commands=commands)[0]
    plan = Planner().build(deployment)

    compose = yaml.safe_load(compose_yaml_for_plan(plan))
    agent_service = next(service for service in compose["services"].values() if service["environment"]["CRAG_ROLE"] == "agent")

    runtime_files = local_runtime_file_contents(plan)
    run_script = runtime_files["local-runtime/run-agent.sh"]

    assert "run_crag_command web_terminal continue 0" in run_script
    assert "printf startup-ok > /workspace/startup.txt" in "\n".join(runtime_files.values())
    assert "startup.ready" in run_script
    assert len(agent_service["command"]) < 100


def test_local_resource_hash_changes_with_startup_commands():
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "none", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "auto_start", "/workspace", terminal=terminal)[0]
    first = DeployNode().build(agent)[0]
    commands = LanguageRuntimeNode().build("nodejs", 22)[0]
    second = DeployNode().build(agent, commands=commands)[0]
    first_plan = Planner().build(first, prompt="Launch terminal.")
    second_plan = Planner().build(second, prompt="Launch terminal.")
    first_agent = next(resource for resource in first_plan.resources if resource.role == "agent")
    second_agent = next(resource for resource in second_plan.resources if resource.role == "agent")

    assert first_agent.desired_hash == second_agent.desired_hash
    assert local_resource_desired_hash(first_agent, first_plan) != local_resource_desired_hash(second_agent, second_plan)


def test_local_resource_hash_ignores_prompt_and_run_id_for_agent_reuse():
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    deployment = DeployNode().build(agent)[0]
    first_plan = Planner().build(deployment, prompt="first prompt", workflow_graph={"same": True})
    second_plan = Planner().build(deployment, prompt="second prompt", workflow_graph={"same": True})
    first_agent = next(resource for resource in first_plan.resources if resource.role == "agent")
    second_agent = next(resource for resource in second_plan.resources if resource.role == "agent")

    assert local_resource_desired_hash(first_agent, first_plan) == local_resource_desired_hash(second_agent, second_plan)


def test_apply_local_runtime_waits_for_terminal_startup(monkeypatch, tmp_path):
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "none", "crag", "secret")[0]
    agent_spec = AgentNode().build("Pi", "model", "auto_start", "/workspace", terminal=terminal)[0]
    commands = LanguageRuntimeNode().build("nodejs", 22)[0]
    deployment = replace(DeployNode().build(agent_spec, commands=commands)[0], reuse_policy="always_create")
    plan = Planner().build(deployment, prompt="Launch terminal.")
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "compose", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": local_resource_desired_hash(agent, plan)}}}]), stderr="")
        if command == ["docker", "logs", "agent1"]:
            return SimpleNamespace(returncode=0, stdout="[crag-local-runtime] startup commands complete\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is False
    assert result.returncode == 0
    assert ["docker", "logs", "agent1"] in calls


def test_agent_compose_command_layers_pod_side_keep_alive():
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    keep_alive = KeepAliveNode().build("time", "terminate", 30, "seconds", 0, 0.0, 0, "pod_side")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    plan = Planner().build(deployment, prompt="wait")

    run_script = local_runtime_file_contents(plan)["local-runtime/run-agent.sh"]

    assert "runpodctl remove pod" in run_script
    assert "RUNPOD_API_KEY" in run_script
    assert "podTerminate" in run_script
    assert "kill -TERM 1" in run_script


def test_agent_auto_start_with_keep_alive_generates_valid_shell():
    agent = AgentNode().build("Pi", "model", "auto_start", "/workspace")[0]
    keep_alive = KeepAliveNode().build("time", "stop", 5, "minutes", 0, 0.0, 0, "both")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    plan = Planner().build(deployment, prompt="run once")
    script = agent_run_script(plan, keep_container_alive=True)

    assert "& &&" not in script
    completed = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr


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


def test_apply_compose_file_reports_missing_engine(monkeypatch, tmp_path):
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda _command: None)

    result = apply_compose_file("containerd", compose_path, project_name="crag-test", action="apply")

    assert result.returncode == 127
    assert "nerdctl" in result.stderr


def test_local_runtime_commands_use_available_podman_and_nerdctl(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda name: f"/usr/bin/{name}")

    assert command_for_engine("podman", "compose.yaml", "crag-test", "apply") == ["podman", "compose", "-f", "compose.yaml", "-p", "crag-test", "up", "-d"]
    assert local_runtime_command("podman", ["ps"]) == ["podman", "ps"]
    assert local_runtime_command("containerd", ["ps"]) == ["nerdctl", "ps"]


def test_local_runtime_commands_report_missing_or_unsupported_engines(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda _command: None)

    with pytest.raises(RuntimeError, match="Podman"):
        command_for_engine("podman", "compose.yaml", "crag-node", "apply")
    with pytest.raises(RuntimeError, match="Podman"):
        local_runtime_command("podman", ["ps"])
    with pytest.raises(RuntimeError, match="Containerd"):
        local_runtime_command("containerd", ["ps"])
    with pytest.raises(RuntimeError, match="Unsupported"):
        command_for_engine("missing", "compose.yaml", "crag-node", "apply")


def test_apply_local_runtime_reuses_matching_agent_container(monkeypatch, tmp_path):
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent_spec = AgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    keep_alive = KeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0)[0]
    deployment = replace(DeployNode().build(agent_spec, keep_alive=keep_alive)[0], reuse_policy="reuse_matching")
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
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "llm", "comfyui-runpod-agentic.desired_hash": local_resource_desired_hash(llm_resource, plan)}}}]), stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": local_resource_desired_hash(agent, plan)}}}]), stderr="")
        if command[:4] == ["docker", "exec", "agent1", "bash"]:
            return SimpleNamespace(returncode=0, stdout="relaunched\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is True
    assert result.action == "reuse"
    assert result.stdout == "relaunched\n"
    assert not any(command[:3] == ["docker", "compose", "-f"] for command in calls)
    assert any(command[:4] == ["docker", "exec", "agent1", "bash"] and "run-agent.sh" in command[-1] for command in calls)


def test_apply_local_runtime_recreates_when_dependency_container_is_missing(monkeypatch, tmp_path):
    llm = LLMServerNode().build("Ollama", "llama3.2", "own_pod", "none")[0]
    agent_spec = AgentNode().build("Pi", "model", "manual", "/workspace", llm=llm)[0]
    deployment = replace(DeployNode().build(agent_spec)[0], reuse_policy="reuse_matching")
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
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": local_resource_desired_hash(agent, plan)}}}]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    _result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is False
    assert ["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "up", "-d"] in calls
    assert not any(command[:4] == ["docker", "exec", "agent1", "bash"] for command in calls)


def test_apply_local_runtime_resumes_stopped_matching_project(monkeypatch, tmp_path):
    agent_spec = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    deployment = replace(DeployNode().build(agent_spec)[0], reuse_policy="resume_stopped")
    plan = Planner().build(deployment, prompt="second prompt")
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []
    started = False

    def fake_run(command, **kwargs):
        nonlocal started
        calls.append(command)
        if command == ["docker", "ps", "--format", "{{json .}}"]:
            stdout = '{"ID":"agent1","Names":"crag-node-agent"}\n' if started else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if command == ["docker", "ps", "-a", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": local_resource_desired_hash(agent, plan)}}}]), stderr="")
        if command[:3] == ["docker", "compose", "-f"]:
            started = True
            return SimpleNamespace(returncode=0, stdout="started\n", stderr="")
        if command[:4] == ["docker", "exec", "agent1", "bash"]:
            return SimpleNamespace(returncode=0, stdout="relaunched\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is True
    assert result.action == "resume_stopped"
    assert "relaunched\n" in result.stdout
    assert ["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "start"] in calls


def test_apply_local_runtime_reconciles_stale_project_before_up(monkeypatch, tmp_path):
    terminal = WebTerminalNode().build("/bin/bash", 7681, 7681, "none", "crag", "secret")[0]
    agent_spec = AgentNode().build("Pi", "model", "manual", "/workspace", terminal=terminal)[0]
    deployment = replace(DeployNode().build(agent_spec)[0], reuse_policy="reuse_matching")
    plan = Planner().build(deployment, prompt="new terminal")
    agent = next(resource for resource in plan.resources if resource.role == "agent")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["docker", "ps", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"stale1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "ps", "-a", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"stale1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "stale1"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {"comfyui-runpod-agentic.role": "agent", "comfyui-runpod-agentic.desired_hash": "old-hash"}}}]), stderr="")
        if command[:3] == ["docker", "compose", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["docker", "logs", "fresh1"]:
            return SimpleNamespace(returncode=0, stdout="[crag-local-runtime] startup commands complete\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_find(engine, project_name, role, desired_hash=None):
        if calls.count(["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "up", "-d"]):
            assert desired_hash == local_resource_desired_hash(agent, plan)
            return "fresh1"
        return None

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.find_local_runtime_container", fake_find)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is False
    assert result.returncode == 0
    assert calls.index(["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "down", "--remove-orphans"]) < calls.index(["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "up", "-d"])


def test_stop_local_runtime_stops_project_orphan_containers(monkeypatch, tmp_path):
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    plan = Planner().build(DeployNode().build(agent)[0], prompt="stop")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["docker", "ps", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"orphan1","Names":"crag-node-old-agent"}\n{"ID":"other1","Names":"other-agent"}\n', stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="stop")

    assert reused is False
    assert result.returncode == 0
    assert ["docker", "compose", "-f", str(compose_path), "-p", "crag-node", "stop"] in calls
    assert ["docker", "stop", "orphan1"] in calls


def test_stop_local_runtime_project_containers_returns_none_without_running_containers(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))

    assert stop_local_runtime_project_containers("docker", "crag-node") is None


def test_list_local_runtime_project_containers_includes_stopped(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='{"ID":"one","Names":"crag-node-agent"}\n{"ID":"two","Names":"other-agent"}\n', stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    assert list_local_runtime_project_containers("docker", "crag-node") == ["one"]
    assert calls == [["docker", "ps", "-a", "--format", "{{json .}}"]]


def test_apply_local_runtime_creates_when_reuse_is_disabled(monkeypatch, tmp_path):
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    deployment = replace(DeployNode().build(agent)[0], reuse_policy="always_create")
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
    assert calls[0] == ["docker", "ps", "-a", "--format", "{{json .}}"]
    assert calls[1][:5] == ["docker", "compose", "-f", str(compose_path), "-p"]


def test_apply_local_runtime_clears_stale_agent_outputs_before_launch(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAG_LOCAL_RUNTIME_STATE_DIR", str(tmp_path / "state"))
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    deployment = replace(DeployNode().build(agent)[0], reuse_policy="always_create")
    plan = Planner().build(deployment, prompt="fresh prompt")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    runtime_dir = tmp_path / "state" / "crag-node" / "runtime"
    runtime_dir.mkdir(parents=True)
    stale_files = ["response.txt", "errors.txt", "agent.log", "startup.ready"]
    for name in stale_files:
        (runtime_dir / name).write_text("stale\n")

    def fake_run(command, **kwargs):
        assert not any((runtime_dir / name).exists() for name in stale_files)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="apply")

    assert reused is False
    assert result.returncode == 0
    assert not any((runtime_dir / name).exists() for name in stale_files)


def test_apply_local_runtime_removes_delete_with_deployment_volumes(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment(retention_policy="delete_with_deployment")
    plan = Planner().build(deployment, prompt="cleanup")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text(compose_yaml_for_plan(plan))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result, reused = apply_local_runtime_plan("docker", compose_path, "crag-node", plan, action="terminate")

    assert reused is False
    assert result.returncode == 0
    assert ["docker", "volume", "rm", "crag-node_vol-workspace"] in calls


def test_network_storage_generates_local_volume_id_without_size():
    storage = NetworkStorageNode().build("", "/workspace", "preserve", 0, "", "crag-workspace", node_id="42")[0]

    assert storage.network_volume_id == "crag-workspace-42"


def test_populate_local_volume_ids_updates_workflow_graph():
    graph = {"42": {"class_type": "NetworkStorage", "inputs": {"network_volume_id": "", "volume_name": "crag-workspace"}}}

    updated = populate_local_volume_ids(graph)

    assert updated["42"]["inputs"]["network_volume_id"] == "crag-workspace-42"


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
    agent = AgentNode().build("Pi", "model", "manual", "/workspace")[0]
    keep_alive = KeepAliveNode().build("time", "stop", 30, "seconds", 0, 0.0, 0, "pod_side")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    plan = Planner().build(deployment, prompt="one turn")
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")
    calls = []

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.Popen", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(pid=123))

    result = enforce_local_keep_alive("docker", compose_path, "crag-node", plan, response_collected=False)

    assert result is None
    assert calls == []


def test_schedule_local_lifecycle_reports_unsupported_engine(tmp_path):
    result = schedule_local_lifecycle("missing", tmp_path / "compose.yaml", "crag-node", "stop", 1)

    assert result.returncode == 127
    assert "Unsupported local runtime engine" in result.stderr


def test_containerd_uses_nerdctl_compose(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: "/usr/bin/nerdctl" if command == "nerdctl" else None)
    monkeypatch.delenv("CRAG_LOCAL_RUNTIME_SUDO", raising=False)

    command = command_for_engine("containerd", "compose.yaml", "crag-test", "apply")

    assert command == ["nerdctl", "compose", "-f", "compose.yaml", "-p", "crag-test", "up", "-d"]


def test_local_runtime_can_prefix_any_engine_with_sudo(monkeypatch):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: "/usr/bin/nerdctl" if command == "nerdctl" else None)
    monkeypatch.setenv("CRAG_LOCAL_RUNTIME_SUDO", "1")

    docker_command = command_for_engine("docker", "compose.yaml", "crag-test", "apply")
    containerd_command = command_for_engine("containerd", "compose.yaml", "crag-test", "terminate")

    assert docker_command == ["sudo", "docker", "compose", "-f", "compose.yaml", "-p", "crag-test", "up", "-d"]
    assert containerd_command == ["sudo", "nerdctl", "compose", "-f", "compose.yaml", "-p", "crag-test", "down", "--remove-orphans"]


def test_apply_node_can_request_sudo(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    apply_path = tmp_path / "sudo.yaml"
    seen = {}

    def fake_run(command, **kwargs):
        if command[:3] == ["sudo", "docker", "ps"] or command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="valid\n", stderr="")

    monkeypatch.delenv("CRAG_LOCAL_RUNTIME_SUDO", raising=False)
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    RunLocalContainersNode().apply(deployment, engine="docker", project_name="crag-node", output_path=str(apply_path), action="apply", use_sudo=True, response_timeout_seconds=0)

    assert seen["command"] == ["sudo", "docker", "compose", "-f", str(apply_path), "-p", "crag-node", "up", "-d"]
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

    result_text, response, errors, _compose_yaml, _saved_path = RunLocalContainersNode().apply(
        deployment,
        engine="docker",
        project_name="crag-node",
        output_path=str(apply_path),
        action="apply",
        response_path="/workspace/result.txt",
    )

    assert json.loads(result_text)["returncode"] == 0
    assert response == "agent response\n"
    assert errors == ""
    assert ["docker", "exec", "agent1", "cat", "/workspace/result.txt"] in calls


def test_apply_node_waits_for_agent_response_instead_of_returning_startup_logs(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    apply_path = tmp_path / "up.yaml"
    cat_calls = 0

    def fake_run(command, **kwargs):
        nonlocal cat_calls
        if command[:3] == ["docker", "ps", "--format"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout='[{"Config":{"Labels":{"comfyui-runpod-agentic.role":"agent"}}}]', stderr="")
        if command == ["docker", "exec", "agent1", "cat", "/workspace/.runpod_agentic/response.txt"]:
            cat_calls += 1
            if cat_calls == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="cat: missing\n")
            return SimpleNamespace(returncode=0, stdout="agent reply\n[crag-agent] complete status=0\n", stderr="")
        if command == ["docker", "logs", "agent1"]:
            return SimpleNamespace(returncode=0, stdout="[crag-local-runtime] startup commands complete\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.time.sleep", lambda _seconds: None)

    _result_text, response, errors, _compose_yaml, _saved_path = RunLocalContainersNode().apply(
        deployment,
        engine="docker",
        project_name="crag-node",
        output_path=str(apply_path),
        action="apply",
        response_path="/workspace/.runpod_agentic/response.txt",
        response_timeout_seconds=1,
    )

    assert response == "agent reply\n[crag-agent] complete status=0\n"
    assert errors == ""
    assert cat_calls == 2


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

    _result_text, response, errors, _compose_yaml, _saved_path = RunLocalContainersNode().apply(
        deployment,
        engine="docker",
        project_name="crag-node",
        output_path=str(apply_path),
        action="apply",
        response_path="/workspace/missing.txt",
        response_timeout_seconds=1,
    )

    assert "script response" in response
    assert errors == ""


def test_read_local_runtime_file_fails_fast_when_container_exited(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["docker", "ps", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["docker", "ps", "-a", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout='{"ID":"agent1","Names":"crag-node-agent"}\n', stderr="")
        if command == ["docker", "inspect", "agent1"]:
            return SimpleNamespace(returncode=0, stdout='[{"Config":{"Labels":{"comfyui-runpod-agentic.role":"agent"}}}]', stderr="")
        if command == ["docker", "logs", "agent1"]:
            return SimpleNamespace(returncode=0, stdout="/bin/bash: line 8: ncu: command not found\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.subprocess.run", fake_run)

    result = read_local_runtime_file("docker", "crag-node", "agent", "/workspace/.runpod_agentic/response.txt", timeout_seconds=900)

    assert result.returncode == 1
    assert "ncu: command not found" in result.stderr
    assert calls == [
        ["docker", "ps", "--format", "{{json .}}"],
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        ["docker", "inspect", "agent1"],
        ["docker", "logs", "agent1"],
    ]


def test_missing_containerd_runtime_returns_error_result(monkeypatch, tmp_path):
    monkeypatch.setattr("comfyui_runpod_agentic.local_runtime.shutil.which", lambda command: None)
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n")

    result = apply_compose_file("containerd", compose_path, project_name="crag-test", action="apply")

    assert result.returncode == 127
    assert "nerdctl" in result.stderr


def test_compose_export_and_apply_nodes_save_files(monkeypatch, tmp_path):
    deployment = build_local_runtime_deployment()
    export_path = tmp_path / "export.yaml"
    apply_path = tmp_path / "apply.yaml"

    compose_yaml, saved_path = ComposeYAMLNode().export(deployment, project_name="crag-node", output_path=str(export_path))

    assert saved_path == str(export_path)
    assert "services:" in compose_yaml
    assert export_path.exists()

    monkeypatch.setattr(
        "comfyui_runpod_agentic.local_runtime.subprocess.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="valid\n", stderr=""),
    )
    result_text, response, errors, apply_yaml, apply_saved_path = RunLocalContainersNode().apply(deployment, engine="docker", project_name="crag-node", output_path=str(apply_path), action="plan")

    assert apply_saved_path == str(apply_path)
    assert response == ""
    assert errors == ""
    assert apply_yaml == apply_path.read_text()
    payload = json.loads(result_text)
    assert payload["command"] == []
    assert payload["action"] == "plan"
    assert "service_count" in payload["stdout"]
