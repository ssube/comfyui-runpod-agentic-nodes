from comfyui_runpod_agentic.config import get_runpod_api_key, get_ssh_env_config
from comfyui_runpod_agentic.nodes import RunpodLogsNode, collect_run_logs
from comfyui_runpod_agentic.runpod_client import RunpodClient
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


def test_logs_node_returns_empty_for_missing_run(monkeypatch, tmp_path):
    monkeypatch.setenv("COMFYUI_USER_DIR", str(tmp_path))

    logs, saved_path = RunpodLogsNode().collect("missing", "stdout", 20000, False)

    assert logs == ""
    assert saved_path == ""
