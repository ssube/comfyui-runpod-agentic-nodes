from pathlib import Path

from comfyui_runpod_agentic.nodes import RunpodAgentNode, RunpodPodNode
from comfyui_runpod_agentic.runner import RunpodRunner
from comfyui_runpod_agentic.state_store import StateStore


class FakeRunpodClient:
    def __init__(self):
        self.created = []
        self.stopped = []

    def create_or_deploy_pod(self, input):
        self.created.append(input)
        return {"id": f"pod-{len(self.created)}", "name": input["name"], "desiredStatus": "RUNNING", "runtime": {"ports": [{"ip": "127.0.0.1", "privatePort": 22, "publicPort": 2222, "type": "tcp"}]}}

    def get_pod(self, pod_id):
        return {"id": pod_id}

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
