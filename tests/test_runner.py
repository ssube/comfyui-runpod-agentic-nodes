import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_runpod_agentic.nodes import (
    AgentNode,
    BrowserNode,
    DeployNode,
    KeepAliveNode,
    LLMApiNode,
    LLMServerNode,
    LocalSQLDatabaseNode,
    MCPServerNode,
    NetworkStorageNode,
    RunOnRunpodNode,
    SkillFrameworkNode,
    SkillNode,
    SSHCommandNode,
    SubagentNode,
    WebTerminalNode,
)
from comfyui_runpod_agentic.planner import Planner
from comfyui_runpod_agentic.runner import (
    RunpodRunner,
    agent_launcher_script,
    first_ready_probe,
    keep_alive_pod_timer_script,
    launcher_runtime_files,
    pi_runtime_files,
    public_http_endpoint,
    public_http_endpoint_for_private_port,
    readiness_probe_paths,
    sanitize_pod_input,
    startup_script_for_plan,
    subagent_runtime_files,
    terminal_auth_for_plan,
    terminal_urls_for_pods,
)
from comfyui_runpod_agentic.ssh_client import CommandResult
from comfyui_runpod_agentic.state_store import StateStore
from comfyui_runpod_agentic.template_resolver import TemplateResolver


class FakeRunpodClient:
    def __init__(self, fail_create=False):
        self.created = []
        self.stopped = []
        self.terminated = []
        self.resumed = []
        self.deleted_network_volumes = []
        self.network_volumes = []
        self.pods = {}
        self.fail_create = fail_create

    def create_or_deploy_pod(self, input):
        self.created.append(input)
        if self.fail_create:
            raise RuntimeError("create failed")
        pod = {"id": f"pod-{len(self.created)}", "name": input["name"], "desiredStatus": "RUNNING", "costPerHr": 0.1, "runtime": {"uptimeInSeconds": 0, "ports": [{"ip": "127.0.0.1", "privatePort": 22, "publicPort": 2222, "type": "tcp"}]}}
        for port in input.get("ports", []):
            private_port = int(port.get("container_port") or 0)
            if private_port and private_port != 22:
                pod["runtime"]["ports"].append({"ip": "127.0.0.1", "privatePort": private_port, "publicPort": private_port, "type": port.get("protocol", "http")})
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
        self.resumed.append(pod_id)
        if pod_id in self.pods:
            self.pods[pod_id]["desiredStatus"] = "RUNNING"
        return {"id": pod_id, "desiredStatus": "RUNNING"}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)
        return None

    def create_network_volume(self, input):
        self.network_volumes.append(input)
        return {"id": f"vol-{len(self.network_volumes)}"}

    def delete_network_volume(self, volume_id):
        self.deleted_network_volumes.append(volume_id)
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
    deployment = DeployNode().build(agent)[0]
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


def test_runner_writes_subagent_config_and_pi_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    subagents = SubagentNode().build("Reviewer", "deepseek-v4-flash", "Return CRAG_SUBAGENT_OK.", node_id="sub1")[0]
    agent = AgentNode().build("Pi", "deepseek-v4-flash", "manual", subagents=subagents)[0]
    deployment = DeployNode().build(agent)[0]
    ssh = FakeSSHClient()
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert "/workspace/.runpod_agentic/subagents.json" in ssh.files
    assert "/workspace/.runpod_agentic/subagents/reviewer.yaml" in ssh.files
    assert "/workspace/.runpod_agentic/subagents/reviewer/SUBAGENT.md" in ssh.files
    assert "/workspace/.runpod_agentic/harness/pi/extensions/crag-subagents/index.ts" in ssh.files
    assert "crag_delegate_subagent" in ssh.files["/workspace/.runpod_agentic/harness/pi/extensions/crag-subagents/index.ts"]


def test_subagent_runtime_files_include_harness_formats():
    env = {
        "CRAG_SUBAGENTS_JSON": '{"subagents":[{"name":"reviewer","model":"deepseek-v4-flash","system_prompt":"Return CRAG_SUBAGENT_OK."}]}',
    }

    files = subagent_runtime_files(env)

    assert files["subagents/reviewer.yaml"].startswith('name: "reviewer"')
    assert "model: deepseek-v4-flash" in files["subagents/reviewer/SUBAGENT.md"]
    assert "crag_delegate_subagent" in files["harness/pi/extensions/crag-subagents/index.ts"]


def test_runner_result_includes_web_terminal_url(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["terminal_urls"] == {"agent": "http://127.0.0.1:7681"}
    assert result["terminal_auth"] == {"agent": {"username": "crag", "password": "secret"}}


def test_runner_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    progress = FakeProgress()
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"), progress=progress)

    runner.run(deployment, mode="apply")

    assert progress.total > 1
    assert "create agent" in progress.messages
    assert "write runtime" in progress.messages
    assert "completed" in progress.messages


def test_runner_reuses_matching_running_pod(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = replace(DeployNode().build(agent)[0], reuse_policy="reuse_matching")
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    first = runner.run(deployment, mode="apply")
    second = runner.run(deployment, mode="apply")

    assert first["pods"] == second["pods"]
    assert len(runpod.created) == 1
    assert any(event["event_type"] == "pod_reused" for event in runner.state_store.list_events(second["run_id"]))


def test_runner_resumes_matching_stopped_pod(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = replace(DeployNode().build(agent)[0], reuse_policy="resume_stopped")
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    first = runner.run(deployment, mode="apply")
    runpod.pods[first["pods"][next(iter(first["pods"]))]]["desiredStatus"] = "EXITED"
    second = runner.run(deployment, mode="apply")

    assert first["pods"] == second["pods"]
    assert len(runpod.created) == 1
    assert runpod.resumed == ["pod-1"]
    assert any(event["event_type"] == "pod_resumed" for event in runner.state_store.list_events(second["run_id"]))


def test_runner_fails_unresolved_template_key_before_create(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Codex", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    runpod = FakeRunpodClient()
    planner = Planner(TemplateResolver(template_ids={}))
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"), planner=planner)

    with pytest.raises(RuntimeError, match="not resolved"):
        runner.run(deployment, mode="apply")

    assert runpod.created == []


def test_runner_materializes_generated_llm_token(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setattr("comfyui_runpod_agentic.runner.first_ready_probe", lambda endpoint, role, env: "/")
    llm = LLMServerNode().build("Ollama", "llama3", "own_pod", "generated_token")[0]
    agent = AgentNode().build("Pi", "model", "manual", llm=llm)[0]
    deployment = DeployNode().build(agent)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    llm_env = runpod.created[0]["env"]
    agent_env = runpod.created[1]["env"]
    assert llm_env["OPENAI_API_KEY"].startswith("crag-")
    assert llm_env["OPENAI_API_KEY"] == agent_env["OPENAI_API_KEY"]
    assert result["plan"]["runtime_contract"]["env"]["values"]["OPENAI_API_KEY"] == llm_env["OPENAI_API_KEY"]


def test_runner_creates_network_volume_from_size(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    storage = NetworkStorageNode().build("", "/workspace", "delete_with_deployment", 25, "US-KS-2", "crag-test")[0]
    deployment = DeployNode().build(agent, network_storage=storage)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert runpod.network_volumes == [{"name": "crag-test", "size": 25, "dataCenterId": "US-KS-2"}]
    assert runpod.created[0]["networkVolumeId"] == "vol-1"
    assert "_networkVolumeSizeGb" not in runpod.created[0]


def test_runner_keeps_setup_stdout_out_of_response(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    command = "printf response && printf warning >&2"
    agent = AgentNode().build("Pi", "model", "manual")[0]
    commands = SSHCommandNode().build(command, "before_start", "fail")[0]
    deployment = DeployNode().build(agent, commands=commands)[0]
    ssh = FakeSSHClient(outputs={command: ("response\n", "warning\n")})
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["response"] == ""
    assert result["errors"] == "warning\n"
    command_logs = runner.state_store.list_commands(result["run_id"])
    assert any(Path(command["stdout_path"]).read_text() == "response\n" for command in command_logs)


def test_runner_executes_command_phases_around_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_LAUNCH_COMMAND", "echo launch-agent")
    before = SSHCommandNode().build("echo before", "before_start", "fail")[0]
    after_start = SSHCommandNode().build("echo after-start", "after_start", "fail", previous=before)[0]
    after_ready = SSHCommandNode().build("echo after-ready", "after_ready", "fail", previous=after_start)[0]
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent, commands=after_ready)[0]
    ssh = FakeSSHClient()
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    before_index = ssh.commands.index("echo before")
    launch_index = next(index for index, command in enumerate(ssh.commands) if "echo launch-agent" in command)
    after_start_index = ssh.commands.index("echo after-start")
    after_ready_index = ssh.commands.index("echo after-ready")
    assert before_index < launch_index < after_start_index < after_ready_index


def test_runner_returns_launch_stdout_and_stderr_when_not_waiting(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_LAUNCH_COMMAND", "echo launch-agent")

    class LaunchOutputSSH(FakeSSHClient):
        def run(self, host, port, command, *, timeout_seconds=None):
            self.commands.append(command)
            if "echo launch-agent" in command:
                return CommandResult(0, "launch stdout\n", "launch stderr\n")
            return CommandResult(0, "", "")

    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=LaunchOutputSSH(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["response"] == "launch stdout\n"
    assert result["errors"] == "launch stderr\n"


def test_runner_fails_when_launch_command_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_LAUNCH_COMMAND", "echo launch-agent")

    class FailingLaunchSSH(FakeSSHClient):
        def run(self, host, port, command, *, timeout_seconds=None):
            self.commands.append(command)
            if "echo launch-agent" in command:
                return CommandResult(2, "", "launch failed\n")
            return CommandResult(0, "", "")

    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FailingLaunchSSH(), state_store=StateStore(tmp_path / "state.sqlite"))

    with pytest.raises(RuntimeError, match="Agent launch failed"):
        runner.run(deployment, mode="apply")

    assert runpod.stopped == ["pod-1"]


def test_runner_retries_retry_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    command = "echo maybe"
    commands = SSHCommandNode().build(command, "before_start", "retry", retry_count=1)[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, commands=commands)[0]
    ssh = FakeSSHClient(outputs={command: [("", "failed\n", 1), ("ok\n", "", 0)]})
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["response"] == ""
    assert ssh.commands.count(command) == 2
    assert any(event["event_type"] == "ssh_command_retry" for event in runner.state_store.list_events(result["run_id"]))


def test_runner_executes_teardown_commands_on_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    teardown = SSHCommandNode().build("echo teardown", "teardown", "fail")[0]
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent, commands=teardown)[0]
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
    deployment = DeployNode().build(agent)[0]
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


def test_runner_apply_and_wait_does_not_return_agent_log_as_response(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_RESPONSE_TIMEOUT_SECONDS", "1")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent)[0]
    ssh = FakeSSHClient(
        outputs={
            "test -s '/workspace/.runpod_agentic/response.txt' && cat '/workspace/.runpod_agentic/response.txt'": ("", "", 1),
            "test -s '/workspace/.runpod_agentic/errors.txt' && cat '/workspace/.runpod_agentic/errors.txt'": ("", "", 1),
            "test -s '/workspace/.runpod_agentic/agent.log' && cat '/workspace/.runpod_agentic/agent.log'": ("setup log\n[crag-agent] complete status=0\n", ""),
        }
    )
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply_and_wait")

    assert result["status"] == "waiting"
    assert result["response"] == ""
    assert any(event["event_type"] == "agent_log_collected" for event in runner.state_store.list_events(result["run_id"]))


def test_runner_apply_and_wait_enforces_turn_keep_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("CRAG_AGENT_RESPONSE_TIMEOUT_SECONDS", "1")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    keep_alive = KeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0, "server_side")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    ssh = FakeSSHClient(outputs={"test -s '/workspace/.runpod_agentic/response.txt' && cat '/workspace/.runpod_agentic/response.txt'": ("one turn\n", "")})
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply_and_wait")

    assert result["keep_alive"] == {"mode": "turns", "action": "stop", "turns": 1.0}
    assert runpod.stopped == ["pod-1"]


def test_runner_schedules_time_keep_alive_after_pod_create(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    scheduled = []
    monkeypatch.setattr(
        "comfyui_runpod_agentic.runner.schedule_runpod_lifecycle",
        lambda pod_id, action, delay_seconds: scheduled.append((pod_id, action, delay_seconds)) or {"pod_id": pod_id, "action": action, "delay_seconds": delay_seconds},
    )
    agent = AgentNode().build("Pi", "model", "manual")[0]
    keep_alive = KeepAliveNode().build("time", "stop", 7, "seconds", 0, 0.0, 0, "server_side")[0]
    startup = SSHCommandNode().build("echo startup-command", "before_start", "fail")[0]
    deployment = DeployNode().build(agent, commands=startup, keep_alive=keep_alive)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert scheduled == [("pod-1", "stop", 7)]
    assert result["scheduled_keep_alive"] == {"pod_id": "pod-1", "action": "stop", "delay_seconds": 7}
    assert any(command == "echo startup-command" for command in runner.ssh_client.commands)


def test_runner_enforces_cost_keep_alive_with_terminate_action(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    keep_alive = KeepAliveNode().build("cost", "terminate", 0, "seconds", 0, 0.1, 0, "server_side")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply")

    result = runner._enforce_response_keep_alive(plan, {"id": "pod-1", "costPerHr": 1.0, "runtime": {"uptimeInSeconds": 3600}}, response_collected=False)

    assert result == {"mode": "cost", "action": "terminate", "estimated_cost_usd": 1.0}
    assert runner.runpod_client.terminated == ["pod-1"]


def test_runner_cost_keep_alive_reports_estimate_below_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    keep_alive = KeepAliveNode().build("cost", "stop", 0, "seconds", 0, 10.0, 0, "server_side")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply")

    result = runner._enforce_response_keep_alive(plan, {"id": "pod-1", "costPerHr": 1.0, "runtime": {"uptimeInSeconds": 3600}}, response_collected=False)

    assert result == {"mode": "cost", "estimated_cost_usd": 1.0}
    assert runner.runpod_client.stopped == []


def test_runner_skips_keep_alive_without_pod_id(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    keep_alive = KeepAliveNode().build("turns", "stop", 0, "seconds", 1, 0.0, 0, "server_side")[0]
    deployment = DeployNode().build(agent, keep_alive=keep_alive)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply")

    assert runner._enforce_response_keep_alive(plan, {}, response_collected=True) is None


def test_run_node_plan_exposes_response_and_errors_slots():
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]

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
    deployment = DeployNode().build(agent)[0]

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
    deployment = DeployNode().build(agent)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    result = runner.run(deployment, mode="apply")

    assert result["status"] == "launched"
    assert len(runpod.created) == 2
    assert result["plan"]["runtime_contract"]["env"]["values"]["PLAYWRIGHT_WS_ENDPOINT"] == "http://127.0.0.1:3000"


def test_runner_dependency_ready_timeout_reports_last_status(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    browser = BrowserNode().build("Playwright", "own_pod", "chromium")[0]
    agent = AgentNode().build("Pi", "model", "manual", browser=browser)[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply")
    resource = next(resource for resource in plan.resources if resource.role == "browser")
    pod = {"id": "pod-browser", "desiredStatus": "STARTING", "runtime": {"ports": []}}

    with pytest.raises(RuntimeError, match="last status"):
        runner._wait_dependency_ready(resource, pod, plan.run_id, "resource-1", timeout_seconds=0.01, interval_seconds=0)


def test_runner_wait_ssh_ready_reports_last_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    ssh = FakeSSHClient(outputs={"true": ("", "not ready", 1)})
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))

    with pytest.raises(RuntimeError, match="not ready"):
        runner._wait_ssh_ready("127.0.0.1", 2222, timeout_seconds=0.01, interval_seconds=0)


def test_runner_wait_agent_response_times_out_with_last_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent)[0]
    ssh = FakeSSHClient(
        outputs={
            "test -s '/workspace/.runpod_agentic/response.txt' && cat '/workspace/.runpod_agentic/response.txt'": ("", "", 1),
            "test -s '/workspace/.runpod_agentic/errors.txt' && cat '/workspace/.runpod_agentic/errors.txt'": ("partial error", "", 0),
            "test -s '/workspace/.runpod_agentic/agent.log' && cat '/workspace/.runpod_agentic/agent.log'": ("", "", 1),
        }
    )
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=ssh, state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply")

    response, errors = runner._wait_agent_response(plan, "127.0.0.1", 2222, timeout_seconds=0.01, interval_seconds=0)

    assert response == ""
    assert errors == "partial error"
    assert any(event["event_type"] == "agent_response_timeout" for event in runner.state_store.list_events(plan.run_id))


def test_runner_logs_sanitized_pod_create_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(fail_create=True), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    with pytest.raises(RuntimeError, match="create failed"):
        runner.run(deployment, mode="apply")

    events = runner.state_store.list_events()
    assert any(event["event_type"] == "pod_create_request" for event in events)
    failed = next(event for event in events if event["event_type"] == "pod_create_failed")
    assert "create failed" in failed["message"]


def test_runner_cleanup_records_stop_and_terminate_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")

    class FailingCleanupClient(FakeRunpodClient):
        def stop_pod(self, pod_id):
            raise RuntimeError("stop failed")

        def terminate_pod(self, pod_id):
            raise RuntimeError("terminate failed")

    agent = AgentNode().build("Pi", "model", "manual")[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FailingCleanupClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply")
    resource = plan.resources[0]
    runner.state_store.record_run(plan.run_id, plan.workflow_hash, plan.deployment_hash, "apply", "started")
    runner.state_store.record_resource(plan.run_id, resource, {"id": "pod-1"}, status="RUNNING")

    runner._cleanup_created(plan, terminate=False)
    runner._cleanup_created(plan, terminate=True)

    event_types = [event["event_type"] for event in runner.state_store.list_events(plan.run_id)]
    assert "cleanup_stop_failed" in event_types
    assert "cleanup_terminate_failed" in event_types


def test_runner_writes_mcp_runtime_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    mcp = MCPServerNode().build("filesystem", "stdio", "npx", "-y @modelcontextprotocol/server-filesystem /workspace", "", "{}", "")[0]
    agent = AgentNode().build("Pi", "model", "manual", system_prompt="Stay concise.", mcp_servers=mcp)[0]
    deployment = DeployNode().build(agent)[0]
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
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert "https://github.com/example/skills.git" in runner.ssh_client.commands[2]
    assert "https://github.com/obra/superpowers.git" in runner.ssh_client.commands[3]


def test_readiness_probe_paths_use_service_health_endpoints():
    assert readiness_probe_paths("llm", {"LLM_PROVIDER": "ollama"}) == ["/api/tags"]
    assert readiness_probe_paths("llm", {"LLM_PROVIDER": "vllm"}) == ["/health", "/v1/models"]
    assert readiness_probe_paths("vector", {"VECTOR_KIND": "qdrant"}) == ["/readyz", "/collections"]
    assert readiness_probe_paths("vector", {"VECTOR_KIND": "chroma"}) == ["/api/v2/heartbeat", "/api/v1/heartbeat"]
    assert readiness_probe_paths("browser", {"BROWSER_KIND": "playwright"}) == ["/json/version", "/"]
    assert readiness_probe_paths("browser", {"BROWSER_KIND": "neko"}) == ["/", "/api/health"]


def test_endpoint_and_terminal_helpers_handle_common_port_shapes():
    pod = {
        "ports": [
            {"host": "ssh.example", "containerPort": 22, "public_port": 2222, "protocol": "tcp"},
            {"hostname": "svc.example", "container_port": 8000, "public_port": 18000, "protocol": "https"},
        ]
    }
    terminal = WebTerminalNode().build("/bin/bash", 7681, 8765, "password", "crag", "secret")[0]
    agent = AgentNode().build("Pi", "model", "manual", terminal=terminal)[0]
    plan = Planner().build(DeployNode().build(agent)[0])
    pods = {plan.resources[0].name: {"runtime": {"ports": [{"ip": "127.0.0.1", "privatePort": 7681, "publicPort": 17681, "type": "https"}]}}}

    assert public_http_endpoint(pod) == "https://svc.example:18000"
    assert public_http_endpoint_for_private_port(pod, 8000) == "https://svc.example:18000"
    assert terminal_urls_for_pods(plan, pods) == {"agent": "https://127.0.0.1:17681"}
    assert terminal_auth_for_plan(plan) == {"agent": {"username": "crag", "password": "secret"}}


def test_sanitize_pod_input_redacts_dict_and_list_env_secrets():
    dict_env = sanitize_pod_input({"env": {"OPENAI_API_KEY": "secret", "MODEL": "qwen"}})
    list_env = sanitize_pod_input({"env": [{"key": "TOKEN", "value": "secret"}, {"key": "MODEL", "value": "qwen"}]})

    assert dict_env["env"] == {"OPENAI_API_KEY": "<redacted>", "MODEL": "qwen"}
    assert list_env["env"][0]["value"] == "<redacted>"
    assert list_env["env"][1]["value"] == "qwen"


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
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="apply", workflow_graph={"test": True})

    assert "echo launch-agent" in runner._launch_command(plan)


def test_launch_command_uses_injected_launcher_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.delenv("CRAG_AGENT_LAUNCH_COMMAND", raising=False)
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent)[0]
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
    assert "launcher.d/20-harness-links.sh" in files
    assert "launcher.d/harnesses/codex.sh" in files
    assert "launcher.d/harnesses/claude.sh" in files
    assert "launcher.d/harnesses/hermes.sh" in files
    assert "launcher.d/harnesses/opencode.sh" in files
    assert "launcher.d/harnesses/pi.sh" in files
    assert "No compatible agent launcher" in files["launcher.d/harnesses/generic.sh"]
    assert 'args=(chat -q "$prompt")' in files["launcher.d/harnesses/hermes.sh"]
    for harness in ("codex", "claude", "opencode", "hermes", "pi"):
        assert "run_harness_command" in files[f"launcher.d/harnesses/{harness}.sh"]


def test_harness_scripts_capture_response_files_for_supported_harnesses(tmp_path):
    files = launcher_runtime_files()
    workspace = tmp_path / "workspace"
    runtime = workspace / ".runpod_agentic"
    bin_dir = tmp_path / "bin"
    harness_dir = runtime / "launcher.d" / "harnesses"
    harness_dir.mkdir(parents=True)
    bin_dir.mkdir()
    (runtime / "prompt.txt").write_text("do the task")
    (runtime / "system_prompt.txt").write_text("be brief")

    for path, content in files.items():
        target = runtime / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o755)

    for harness in ("codex", "claude", "opencode", "hermes", "pi"):
        binary = bin_dir / harness
        binary.write_text("#!/usr/bin/env bash\nprintf '%s argv: %s\\n' \"$(basename \"$0\")\" \"$*\"\n")
        binary.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "WORKSPACE_DIR": str(workspace),
            "CRAG_RUNTIME_DIR": str(runtime),
            "AGENT_HARNESS": harness,
            "AGENT_MODEL": "model-1",
            "AGENT_PROMPT_FILE": str(runtime / "prompt.txt"),
            "AGENT_SYSTEM_PROMPT_FILE": str(runtime / "system_prompt.txt"),
            "MCP_SERVERS_FILE": str(runtime / "mcp_servers.json"),
        }

        result = subprocess.run(["bash", str(harness_dir / f"{harness}.sh")], env=env, text=True, capture_output=True, check=False)

        response = (runtime / "response.txt").read_text()
        assert result.returncode == 0, result.stderr
        assert f"harness: {harness}" in response
        assert f"{harness} argv:" in response
        if harness in {"claude", "pi"}:
            assert "--system-prompt be brief" in response
        else:
            assert "--system-prompt" not in response
        assert "[crag-agent] complete status=0" in response
        assert (runtime / "errors.txt").exists()
        (runtime / "response.txt").unlink()
        (runtime / "errors.txt").unlink()


def test_harness_scripts_pass_supported_keep_alive_cli_args_and_warn_for_unsupported(tmp_path):
    files = launcher_runtime_files()
    workspace = tmp_path / "workspace"
    runtime = workspace / ".runpod_agentic"
    bin_dir = tmp_path / "bin"
    harness_dir = runtime / "launcher.d" / "harnesses"
    harness_dir.mkdir(parents=True)
    bin_dir.mkdir()
    (runtime / "prompt.txt").write_text("do the task")
    (runtime / "system_prompt.txt").write_text("be brief")

    for path, content in files.items():
        target = runtime / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o755)

    for harness in ("codex", "claude", "opencode", "hermes", "pi"):
        binary = bin_dir / harness
        binary.write_text("#!/usr/bin/env bash\nprintf '%s argv: %s\\n' \"$(basename \"$0\")\" \"$*\"\n")
        binary.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "WORKSPACE_DIR": str(workspace),
            "CRAG_RUNTIME_DIR": str(runtime),
            "AGENT_HARNESS": harness,
            "AGENT_MODEL": "model-1",
            "AGENT_PROMPT_FILE": str(runtime / "prompt.txt"),
            "AGENT_SYSTEM_PROMPT_FILE": str(runtime / "system_prompt.txt"),
            "MCP_SERVERS_FILE": str(runtime / "mcp_servers.json"),
            "CRAG_KEEP_ALIVE_TURN_LIMIT": "3",
            "CRAG_KEEP_ALIVE_COST_LIMIT_USD": "0.25",
        }

        result = subprocess.run(["bash", str(harness_dir / f"{harness}.sh")], env=env, text=True, capture_output=True, check=False)
        response = (runtime / "response.txt").read_text()
        errors = (runtime / "errors.txt").read_text()

        assert result.returncode == 0, result.stderr
        if harness in {"claude", "hermes"}:
            assert "--max-turns 3" in response
            assert "--max-budget-usd 0.25" in response
            assert "[crag-keepalive]" not in errors
        else:
            assert "--max-turns" not in response
            assert "--max-budget-usd" not in response
            assert f"{harness} does not support native turn limits" in errors
            assert f"{harness} does not support native cost limits" in errors
        (runtime / "response.txt").unlink()
        (runtime / "errors.txt").unlink()


def test_harness_links_script_projects_central_skills(tmp_path):
    files = launcher_runtime_files()
    workspace = tmp_path / "workspace"
    runtime = workspace / ".runpod_agentic"
    legacy = workspace / ".codex" / "skills"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("legacy")
    env = {
        **os.environ,
        "WORKSPACE_DIR": str(workspace),
        "CRAG_RUNTIME_DIR": str(runtime),
        "AGENT_PROMPT_FILE": str(runtime / "prompt.txt"),
        "AGENT_SYSTEM_PROMPT_FILE": str(runtime / "system_prompt.txt"),
        "MCP_SERVERS_FILE": str(runtime / "mcp_servers.json"),
        "HOME": str(tmp_path / "home"),
    }

    subprocess.run(["bash", "-c", files["launcher.d/20-harness-links.sh"]], env=env, text=True, capture_output=True, check=True)

    assert legacy.is_symlink()
    assert legacy.resolve() == runtime / "skills"
    assert (runtime / "skills" / "SKILL.md").read_text() == "legacy"
    assert (tmp_path / "home" / ".agents" / "skills").resolve() == runtime / "skills"


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
    deployment = DeployNode().build(agent)[0]
    runpod = FakeRunpodClient()
    runner = RunpodRunner(runpod_client=runpod, ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert any(path.endswith("harness/pi/models.json") for path in runner.ssh_client.files)
    assert any(path.endswith("harness/pi/providers.json") for path in runner.ssh_client.files)


def test_runner_writes_runtime_contract_skill_files(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    sql = LocalSQLDatabaseNode().build("SQLite", "app", "/workspace/db/app.sqlite")[0]
    agent = AgentNode().build("Pi", "model", "manual", sql_database=sql)[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))

    runner.run(deployment, mode="apply")

    assert any(path.endswith("/skills/crag-database/SKILL.md") for path in runner.ssh_client.files)
    skill_path = next(path for path in runner.ssh_client.files if path.endswith("/skills/crag-database/SKILL.md"))
    assert runner.ssh_client.files[skill_path].startswith("---\nname: crag-database\n")


def test_startup_script_for_plan_is_pasteable_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    agent = AgentNode().build("Pi", "model", "wait_for_commands")[0]
    deployment = DeployNode().build(agent)[0]
    runner = RunpodRunner(runpod_client=FakeRunpodClient(), ssh_client=FakeSSHClient(), state_store=StateStore(tmp_path / "state.sqlite"))
    plan = runner.planner.build(deployment, mode="plan", prompt="Do it.", workflow_graph={"test": True})

    script = startup_script_for_plan(plan)

    assert script.startswith("bash <<'CRAG_STARTUP'")
    assert ".runpod_agentic/launcher.sh" in script
    assert "nohup .runpod_agentic/launcher.sh" in script
    assert script.endswith("CRAG_STARTUP")
