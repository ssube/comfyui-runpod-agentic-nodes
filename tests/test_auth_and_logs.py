from comfyui_runpod_agentic import config as config_module
from comfyui_runpod_agentic.config import RunpodAuthConfig, get_runpod_api_key, get_ssh_env_config
from comfyui_runpod_agentic.nodes import LogsNode, collect_run_logs
from comfyui_runpod_agentic.runpod_client import RunpodClient
from comfyui_runpod_agentic.ssh_client import CommandResult
from comfyui_runpod_agentic.state_store import StateStore


def test_runpod_client_loads_token_from_env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_test_token")

    assert get_runpod_api_key() == "rp_test_token"
    assert RunpodClient().api_key == "rp_test_token"


def test_runpod_client_loads_token_from_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "runpod.env"
    env_file.write_text("export RUNPOD_API_KEY='rp_file_token'\n")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setenv("RUNPOD_ENV_FILE", str(env_file))

    assert get_runpod_api_key() == "rp_file_token"
    assert RunpodClient().api_key == "rp_file_token"


def test_runpod_client_loads_token_from_repo_relative_env_file(monkeypatch, tmp_path):
    package_dir = tmp_path / "comfyui_runpod_agentic"
    package_dir.mkdir()
    env_file = tmp_path / ".env.d" / "runpod.env"
    env_file.parent.mkdir()
    env_file.write_text("RUNPOD_API_KEY=rp_repo_token\n")
    monkeypatch.setattr(config_module, "__file__", str(package_dir / "config.py"))
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_ENV_FILE", raising=False)

    config = RunpodAuthConfig(default_env_file=".env.d/runpod.env")

    assert get_runpod_api_key(config) == "rp_repo_token"


def test_ssh_config_loads_proxy_suffix_from_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "runpod.env"
    env_file.write_text("RUNPOD_SSH_PROXY_SUFFIX=64410ecc\nRUNPOD_SSH_PRIVATE_KEY_PATH=/tmp/key\n")
    monkeypatch.setenv("RUNPOD_ENV_FILE", str(env_file))
    monkeypatch.delenv("RUNPOD_SSH_PROXY_SUFFIX", raising=False)

    values = get_ssh_env_config()

    assert values["proxy_suffix"] == "64410ecc"
    assert values["private_key_path"] == "/tmp/key"


def test_collect_run_logs_reads_command_log_files(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("hello\n")
    stderr.write_text("warning\n")
    command_id = store.start_command("run1", "resource1", "before_start", 1, "hash", str(stdout), str(stderr))
    store.finish_command(command_id, "completed", 0)

    text = collect_run_logs(store, "run1", stream="both", max_chars=20000)

    assert "hello" in text
    assert "warning" in text


def test_collect_run_logs_truncates_to_tail(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    stdout = tmp_path / "stdout.log"
    stdout.write_text("prefix\n" + "x" * 80 + "\ntail\n")
    command_id = store.start_command("run1", "resource1", "before_start", 1, "hash", str(stdout), "")
    store.finish_command(command_id, "completed", 0)

    text = collect_run_logs(store, "run1", stream="stdout", max_chars=20)

    assert text == ("x" * 14 + "\ntail\n")


def test_collect_run_logs_reads_remote_agent_log(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    pod = {
        "id": "pod-1",
        "name": "crag-workflow-agent-node-deadbeef",
        "desiredStatus": "RUNNING",
        "env": [{"key": "CRAG_RUN_ID", "value": "run1"}, {"key": "CRAG_ROLE", "value": "agent"}],
        "runtime": {"ports": [{"ip": "127.0.0.1", "privatePort": 22, "publicPort": 2222, "type": "tcp"}]},
    }
    store.record_remote_resource(pod)

    class FakeRunpod:
        def get_pod(self, pod_id):
            assert pod_id == "pod-1"
            return pod

    class FakeSSH:
        def run(self, host, port, command, *, timeout_seconds=None):
            if "agent.log" in command:
                return CommandResult(0, "agent output\n", "")
            return CommandResult(1, "", "")

    text = collect_run_logs(store, "run1", stream="both", max_chars=20000, runpod_client=FakeRunpod(), ssh_client=FakeSSH())

    assert "remote agent log" in text
    assert "agent output" in text


def test_collect_run_logs_reports_unavailable_remote_agent_log(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    pod = {
        "id": "pod-1",
        "name": "crag-workflow-agent-node-deadbeef",
        "desiredStatus": "RUNNING",
        "env": [{"key": "CRAG_RUN_ID", "value": "run1"}, {"key": "CRAG_ROLE", "value": "agent"}],
        "runtime": {"ports": []},
    }
    store.record_remote_resource(pod)

    class FakeRunpod:
        def get_pod(self, pod_id):
            assert pod_id == "pod-1"
            return pod

    class FakeSSH:
        def run(self, host, port, command, *, timeout_seconds=None):
            raise AssertionError("unreachable without an SSH endpoint")

    text = collect_run_logs(store, "run1", stream="both", max_chars=20000, runpod_client=FakeRunpod(), ssh_client=FakeSSH())

    assert "remote agent logs unavailable (pod-1)" in text


def test_logs_node_returns_empty_for_missing_run(monkeypatch, tmp_path):
    monkeypatch.setenv("COMFYUI_USER_DIR", str(tmp_path))

    logs, saved_path = LogsNode().collect("missing", "stdout", 20000, save_copy=False)

    assert logs == ""
    assert saved_path == ""
