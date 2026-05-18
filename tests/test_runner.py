from pathlib import Path

import pytest

from comfyui_runpod_agentic.nodes import (
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodMCPServerNode,
    RunpodPodNode,
    RunpodSkillFrameworkNode,
    RunpodSkillNode,
)
from comfyui_runpod_agentic.runner import (
    RunpodRunner,
    agent_launcher_script,
    first_ready_probe,
    launcher_runtime_files,
    readiness_probe_paths,
    startup_script_for_plan,
)
from comfyui_runpod_agentic.state_store import StateStore


class FakeRunpodClient:
    def __init__(self, fail_create=False):
        self.created = []
        self.stopped = []
        self.pods = {}
        self.fail_create = fail_create

    def create_or_deploy_pod(self, input):
        self.created.append(input)
        if self.fail_create:
            raise RuntimeError("create failed")
        pod = {"id": f"pod-{len(self.created)}", "name": input["name"], "desiredStatus": "RUNNING", "runtime": {"ports": [{"ip": "127.0.0.1", "privatePort": 22, "publicPort": 2222, "type": "tcp"}]}}
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
        return None


class FakeSSHClient:
    def __init__(self):
        self.commands = []
        self.files = {}

    def run(self, host, port, command, *, timeout_seconds=None):
        self.commands.append(command)
        return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    def write_file(self, host, port, path, content):
        self.files[path] = content


def test_runner_apply_uses_injected_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = RunpodAgentNode().build("Pi", "model", "manual")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
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
    assert runner.state_store.list_commands(result["run_id"]) == []


def test_runner_apply_waits_for_dependency_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("RUNPOD_DEPENDENCY_READY_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr("comfyui_runpod_agentic.runner.first_ready_probe", lambda endpoint, role, env: "/")
    browser = RunpodBrowserNode().build("Playwright", "own_pod", "chromium")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", browser=browser)[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["status"] == "launched"
    assert len(runpod.created) == 2
    assert result["plan"]["runtime_contract"]["env"]["values"]["PLAYWRIGHT_WS_ENDPOINT"] == "http://127.0.0.1:3000"


def test_runner_logs_sanitized_pod_create_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = RunpodAgentNode().build("Pi", "model", "manual")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(fail_create=True), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    with pytest.raises(RuntimeError, match="create failed"):
        runner.run(deployment, mode="apply")

    events = runner.state_store.list_events()
    assert any(event["event_type"] == "pod_create_request" for event in events)
    failed = next(event for event in events if event["event_type"] == "pod_create_failed")
    assert "create failed" in failed["message"]


def test_runner_writes_mcp_runtime_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    mcp = RunpodMCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", system_prompt="Stay concise.", mcp_servers=mcp)[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
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
    skill = RunpodSkillNode().build("frontend-design", "https://github.com/example/skills.git", "frontend-design", "", "main")[0]
    skills = RunpodSkillFrameworkNode().build("Superpowers", "", "", previous=skill)[0]
    agent = RunpodAgentNode().build("Pi", "model", "manual", skills=skills)[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert "https://github.com/example/skills.git" in runner.ssh_client.commands[1]
    assert "https://github.com/obra/superpowers.git" in runner.ssh_client.commands[2]


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
    agent = RunpodAgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply", workflow_graph={"test": True})

    assert "echo launch-agent" in runner._launch_command(plan)


def test_launch_command_uses_injected_launcher_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.delenv("CRAG_AGENT_LAUNCH_COMMAND", raising=False)
    agent = RunpodAgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
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
    assert "launcher.d/harnesses/opencode.sh" in files
    assert "No compatible agent launcher" in files["launcher.d/harnesses/generic.sh"]


def test_startup_script_for_plan_is_pasteable_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = RunpodAgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="plan", prompt="Do it.", workflow_graph={"test": True})

    script = startup_script_for_plan(plan)

    assert script.startswith("bash <<'CRAG_STARTUP'")
    assert ".runpod_agentic/launcher.sh" in script
    assert "nohup .runpod_agentic/launcher.sh" in script
    assert script.endswith("CRAG_STARTUP")
