from pathlib import Path

import pytest

from comfyui_runpod_agentic.nodes import RunpodAgentNode, RunpodBrowserNode, RunpodMCPServerNode, RunpodPodNode
from comfyui_runpod_agentic.runner import RunpodRunner
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
    assert any(resource["runpod_pod_id"] == "orphan" for resource in runner.state_store.list_resources())
    assert runner.state_store.list_commands(result["run_id"]) == []


def test_runner_apply_waits_for_dependency_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("RUNPOD_DEPENDENCY_READY_TIMEOUT_SECONDS", "10")
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
    agent = RunpodAgentNode().build("Pi", "model", "manual", mcp_servers=mcp)[0]
    deployment = RunpodPodNode().build(agent, gpu_count=0)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["status"] == "launched"
    mcp_paths = [path for path in runner.ssh_client.files if path.endswith("mcp_servers.json")]
    assert mcp_paths
    assert "filesystem" in runner.ssh_client.files[mcp_paths[0]]
