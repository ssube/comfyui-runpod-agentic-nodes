from pathlib import Path

import pytest

from comfyui_runpod_agentic.nodes import (
    AgentNode,
    BrowserNode,
    DeployNode,
    KeepAliveNode,
    LLMApiNode,
    MCPServerNode,
    RunOnRunpodNode,
    SkillFrameworkNode,
    SkillNode,
    SSHCommandNode,
)
from comfyui_runpod_agentic.runner import (
    RunpodRunner,
    agent_launcher_script,
    first_ready_probe,
    keep_alive_pod_timer_script,
    launcher_runtime_files,
    pi_runtime_files,
    readiness_probe_paths,
    startup_script_for_plan,
)
from comfyui_runpod_agentic.ssh_client import CommandResult
from comfyui_runpod_agentic.state_store import StateStore


class FakeRunpodClient:
    def __init__(self, fail_create=False):
        self.created = []
        self.stopped = []
        self.terminated = []
        self.pods = {}
        self.fail_create = fail_create

    def create_or_deploy_pod(self, input):
        self.created.append(input)
        if self.fail_create:
            raise RuntimeError("create failed")
        pod = {"id": f"pod-{len(self.created)}", "name": input["name"], "desiredStatus": "RUNNING", "costPerHr": 0.1, "runtime": {"uptimeInSeconds": 0, "ports": [{"ip": "127.0.0.1", "privatePort": 22, "publicPort": 2222, "type": "tcp"}]}}
        if any(port.get("container_port") == 3000 for port in input.get("ports", [])):
            pod["runtime"]["ports"].append({"ip": "127.0.0.1", "privatePort": 3000, "publicPort": 3000, "type": "http"})
        self.pods[pod["id"]] = pod
        return pod

    def get_pod(self, pod_id):
        return self.pods.get(pod_id, {"id": pod_id})

    def list_pods(self):
        return [{"id": "orphan", "name": "crag-workflow-agent-node-deadbeef", "desiredStatus": "RUNNING"}]

    def stop_pod(self, pod_id):
        self.stopped.append(pod_id)
        return {"id": pod_id, "desiredStatus": "EXITED"}

    def resume_pod(self, pod_id):
        return {"id": pod_id, "desiredStatus": "RUNNING"}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)
        return None


class FakeSSHClient:
    def __init__(self, outputs=None):
        self.commands = []
        self.files = {}
        self.outputs = outputs or {}

    def run(self, host, port, command, *, timeout_seconds=None):
        self.commands.append(command)
        output = self.outputs.get(command, ("", ""))
        if isinstance(output, list):
            stdout, stderr, exit_code = output.pop(0)
            return CommandResult(exit_code, stdout, stderr)
        if len(output) == 3:
            stdout, stderr, exit_code = output
            return CommandResult(exit_code, stdout, stderr)
        stdout, stderr = output
        return CommandResult(0, stdout, stderr)

    def write_file(self, host, port, path, content):
        self.files[path] = content


class FakeProgress:
    def __init__(self):
        self.total = 0
        self.messages = []

    def set_total(self, total):
        self.total = total

    def update(self, message=""):
        self.messages.append(message)


def test_runner_apply_uses_injected_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runpod = FakeRunpodClient()
    ssh = FakeSSHClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["status"] == "launched"
    assert len(runpod.created) == 1
    assert any(Path(path).name == "session.env" for path in ssh.files)
    assert any(Path(path).name == "launcher.sh" for path in ssh.files)
    assert any(path.endswith("launcher.d/harnesses/codex.sh") for path in ssh.files)
    assert any(resource["runpod_pod_id"] == "orphan" for resource in runner.state_store.list_resources())
    commands = runner.state_store.list_commands(result["run_id"])
    assert len(commands) == 1
    assert any(command.endswith("pi --help >/dev/null") for command in ssh.commands)


def test_runner_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    progress = FakeProgress()
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"), progress=progress)

    runner.run(deployment, mode="apply")

    assert progress.total > 1
    assert "create agent" in progress.messages
    assert "write runtime" in progress.messages
    assert "completed" in progress.messages


def test_runner_result_exposes_response_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    command = "printf response && printf warning >&2"
    agent = AgentNode().build("Pi", "model", "manual")[0]
    commands = SSHCommandNode().build(command, "before_start", "fail")[0]
    deployment = DeployNode().build(agent, gpu_count=0, commands=commands)[0]
    ssh = FakeSSHClient(outputs={command: ("response\n", "warning\n")})
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["response"] == "response\n"
    assert result["errors"] == "warning\n"


def test_runner_executes_command_phases_around_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_LAUNCH_COMMAND", "echo launch-agent")
    before = SSHCommandNode().build("echo before", "before_start", "fail")[0]
    after_start = SSHCommandNode().build("echo after-start", "after_start", "fail", previous=before)[0]
    after_ready = SSHCommandNode().build("echo after-ready", "after_ready", "fail", previous=after_start)[0]
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent, gpu_count=0, commands=after_ready)[0]
    ssh = FakeSSHClient()
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    before_index = ssh.commands.index("echo before")
    launch_index = next(index for index, command in enumerate(ssh.commands) if "echo launch-agent" in command)
    after_start_index = ssh.commands.index("echo after-start")
    after_ready_index = ssh.commands.index("echo after-ready")
    assert before_index < launch_index < after_start_index < after_ready_index


def test_runner_retries_retry_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    command = "echo maybe"
    commands = SSHCommandNode().build(command, "before_start", "retry", retry_count=1)[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0, commands=commands)[0]
    ssh = FakeSSHClient(outputs={command: [("", "failed\n", 1), ("ok\n", "", 0)]})
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["response"].endswith("ok\n")
    assert ssh.commands.count(command) == 2
    assert any(event["event_type"] == "ssh_command_retry" for event in runner.state_store.list_events(result["run_id"]))


def test_runner_executes_teardown_commands_on_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    teardown = SSHCommandNode().build("echo teardown", "teardown", "fail")[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0, commands=teardown)[0]
    runpod = FakeRunpodClient()
    ssh = FakeSSHClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))
    runner.run(deployment, mode="apply")

    result = runner.run(deployment, mode="stop")

    assert result["status"] == "stop"
    assert "echo teardown" in ssh.commands
    assert runpod.stopped == ["pod-1"]


def test_runner_apply_and_wait_collects_agent_response_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_RESPONSE_TIMEOUT_SECONDS", "1")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    ssh = FakeSSHClient(
        outputs={
            "test -s '/workspace/.runpod_agentic/response.txt' && cat '/workspace/.runpod_agentic/response.txt'": ("agent done\n", ""),
            "test -s '/workspace/.runpod_agentic/errors.txt' && cat '/workspace/.runpod_agentic/errors.txt'": ("", ""),
        }
    )
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply_and_wait")

    assert result["status"] == "completed"
    assert result["response"].endswith("agent done\n")
    assert any(event["event_type"] == "agent_response_collected" for event in runner.state_store.list_events(result["run_id"]))


def test_runner_apply_and_wait_enforces_turn_keep_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_RESPONSE_TIMEOUT_SECONDS", "1")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    keep_alive = KeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0, "server_side")[0]
    deployment = DeployNode().build(agent, gpu_count=0, keep_alive=keep_alive)[0]
    ssh = FakeSSHClient(outputs={"test -s '/workspace/.runpod_agentic/response.txt' && cat '/workspace/.runpod_agentic/response.txt'": ("one turn\n", "")})
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply_and_wait")

    assert result["keep_alive"] == {"mode": "turns", "action": "stop", "turns": 1.0}
    assert runpod.stopped == ["pod-1"]


def test_run_node_plan_exposes_response_and_errors_slots():
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]

    result, response, errors = RunOnRunpodNode().run(deployment, mode="plan")

    assert '"resources"' in result
    assert response == ""
    assert errors == ""


def test_run_node_apply_returns_errors_without_losing_output_slots(monkeypatch):
    class FailingRunner:
        def run(self, *_args, **_kwargs):
            raise RuntimeError("remote apply failed")

    monkeypatch.setattr("comfyui_runpod_agentic.runner.RunpodRunner", FailingRunner)
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]

    result, response, errors = RunOnRunpodNode().run(deployment, mode="apply")

    assert '"status": "failed"' in result
    assert response == ""
    assert errors == "remote apply failed"


def test_runner_apply_waits_for_dependency_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("RUNPOD_DEPENDENCY_READY_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr("comfyui_runpod_agentic.runner.first_ready_probe", lambda endpoint, role, env: "/")
    browser = BrowserNode().build("Playwright", "own_pod", "chromium")[0]
    agent = AgentNode().build("Pi", "model", "manual", browser=browser)[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["status"] == "launched"
    assert len(runpod.created) == 2
    assert result["plan"]["runtime_contract"]["env"]["values"]["PLAYWRIGHT_WS_ENDPOINT"] == "http://127.0.0.1:3000"


def test_runner_logs_sanitized_pod_create_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(fail_create=True), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    with pytest.raises(RuntimeError, match="create failed"):
        runner.run(deployment, mode="apply")

    events = runner.state_store.list_events()
    assert any(event["event_type"] == "pod_create_request" for event in events)
    failed = next(event for event in events if event["event_type"] == "pod_create_failed")
    assert "create failed" in failed["message"]


def test_runner_writes_mcp_runtime_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    mcp = MCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    agent = AgentNode().build("Pi", "model", "manual", system_prompt="Stay concise.", mcp_servers=mcp)[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply", prompt="List available files.")

    assert result["status"] == "launched"
    mcp_paths = [path for path in runner.ssh_client.files if path.endswith("mcp_servers.json")]
    assert mcp_paths
    assert "filesystem" in runner.ssh_client.files[mcp_paths[0]]
    assert any(path.endswith("system_prompt.txt") for path in runner.ssh_client.files)
    assert any(path.endswith("prompt.txt") for path in runner.ssh_client.files)


def test_runner_installs_skills_before_user_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    skill = SkillNode().build("frontend-design", "https://github.com/example/skills.git", "frontend-design", "", "main")[0]
    skills = SkillFrameworkNode().build("Superpowers", "", "", previous=skill)[0]
    agent = AgentNode().build("Pi", "model", "manual", skills=skills)[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert "https://github.com/example/skills.git" in runner.ssh_client.commands[2]
    assert "https://github.com/obra/superpowers.git" in runner.ssh_client.commands[3]


def test_readiness_probe_paths_use_service_health_endpoints():
    assert readiness_probe_paths("llm", {"LLM_PROVIDER": "ollama"}) == ["/api/tags"]
    assert readiness_probe_paths("vector", {"VECTOR_PROVIDER": "qdrant"}) == ["/readyz", "/collections"]


def test_first_ready_probe_accepts_successful_http_status(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        assert request.full_url == "http://127.0.0.1:11434/api/tags"
        assert timeout == 3.0
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert first_ready_probe("http://127.0.0.1:11434", "llm", {"LLM_PROVIDER": "ollama"}) == "/api/tags"


def test_launch_command_can_use_configured_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_LAUNCH_COMMAND", "echo launch-agent")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply", workflow_graph={"test": True})

    assert "echo launch-agent" in runner._launch_command(plan)


def test_launch_command_uses_injected_launcher_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.delenv("CRAG_AGENT_LAUNCH_COMMAND", raising=False)
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply", workflow_graph={"test": True})

    command = runner._launch_command(plan)

    assert ".runpod_agentic/launcher.sh" in command
    assert "/usr/local/bin/runpod-agent-launch" not in command


def test_injected_launcher_documents_override_path():
    script = agent_launcher_script()

    assert "CRAG_AGENT_LAUNCH_COMMAND" in script
    assert "runpod-agent-launch" in script
    assert "harnesses/generic.sh" in script


def test_launcher_runtime_files_include_common_harness_stubs():
    files = launcher_runtime_files()

    assert "launcher.sh" in files
    assert "launcher.d/00-env.sh" in files
    assert "launcher.d/10-preflight.sh" in files
    assert "launcher.d/harnesses/codex.sh" in files
    assert "launcher.d/harnesses/claude.sh" in files
    assert "launcher.d/harnesses/hermes.sh" in files
    assert "launcher.d/harnesses/opencode.sh" in files
    assert "launcher.d/harnesses/pi.sh" in files
    assert "No compatible agent launcher" in files["launcher.d/harnesses/generic.sh"]
    assert 'args=(chat -q "$prompt")' in files["launcher.d/harnesses/hermes.sh"]


def test_pi_runtime_files_configure_ollama_cloud():
    files = pi_runtime_files({"LLM_PROVIDER": "ollama_cloud", "OLLAMA_HOST": "https://ollama.com", "OLLAMA_MODEL": "deepseek-v4-flash"})

    assert "harness/pi/models.json" in files
    assert "harness/pi/providers.json" in files
    assert "deepseek-v4-flash" in files["harness/pi/models.json"]
    assert '"apiKey": "OLLAMA_CLOUD_API_KEY"' in files["harness/pi/models.json"]


def test_keep_alive_pod_timer_layers_runpodctl_graphql_and_process_fallback():
    policy = KeepAliveNode().build("time", "terminate", 30, "seconds", 0, 0.0, 0, "pod_side")[0]

    script = keep_alive_pod_timer_script(policy)

    assert "runpodctl remove pod" in script
    assert "RUNPOD_POD_ID" in script
    assert "RUNPOD_API_KEY" in script
    assert "podTerminate" in script
    assert "kill -TERM 1" in script


def test_runner_writes_pi_runtime_config_files(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    llm = LLMApiNode().build("Ollama Cloud", "deepseek-v4-flash", "OLLAMA_API_KEY")[0]
    agent = AgentNode().build("Pi", "deepseek-v4-flash", "manual", llm=llm)[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert any(path.endswith("harness/pi/models.json") for path in runner.ssh_client.files)
    assert any(path.endswith("harness/pi/providers.json") for path in runner.ssh_client.files)
    agent_env = runpod.created[-1]["env"]
    assert agent_env["OLLAMA_API_KEY"] == "{{ RUNPOD_SECRET_OLLAMA_API_KEY }}"
    assert agent_env["OLLAMA_CLOUD_API_KEY"] == "{{ RUNPOD_SECRET_OLLAMA_API_KEY }}"


def test_startup_script_for_plan_is_pasteable_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="plan", prompt="Do it.", workflow_graph={"test": True})

    script = startup_script_for_plan(plan)

    assert script.startswith("bash <<'CRAG_STARTUP'")
    assert ".runpod_agentic/launcher.sh" in script
    assert "nohup .runpod_agentic/launcher.sh" in script
    assert script.endswith("CRAG_STARTUP")
