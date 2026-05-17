from comfyui_runpod_agentic.runpod_client import clean_none, endpoint_with_api_key, normalize_pod_input
from comfyui_runpod_agentic.ssh_client import CommandResult, extract_ssh_endpoint, normalize_ssh_result, runpod_proxy_ssh_endpoint


def test_clean_none_removes_nested_none_values():
    assert clean_none({"a": 1, "b": None, "c": {"d": None, "e": 2}}) == {"a": 1, "c": {"e": 2}}


def test_extract_ssh_endpoint_from_runtime_ports():
    pod = {"runtime": {"ports": [{"ip": "1.2.3.4", "privatePort": 22, "publicPort": 22022, "type": "tcp"}]}}

    assert extract_ssh_endpoint(pod) == ("1.2.3.4", 22022)


def test_runpod_proxy_ssh_endpoint_uses_pod_id():
    assert runpod_proxy_ssh_endpoint({"id": "pod123"}, "abc") == ("pod123-abc@ssh.runpod.io", 22)


def test_normalize_ssh_result_rejects_runpod_pty_error():
    result = normalize_ssh_result(CommandResult(0, "Error: Your SSH client doesn't support PTY", ""))

    assert result.exit_code == 255


def test_endpoint_with_api_key_uses_query_parameter():
    url = endpoint_with_api_key("https://api.runpod.io/graphql?x=1", "token")

    assert url == "https://api.runpod.io/graphql?x=1&api_key=token"


def test_normalize_pod_input_converts_env_and_ports():
    data = normalize_pod_input(
        {
            "env": {"B": "2", "A": "1"},
            "ports": [{"container_port": 8000, "protocol": "http"}, {"container_port": 22, "protocol": "tcp"}],
        }
    )

    assert data["env"] == [{"key": "A", "value": "1"}, {"key": "B", "value": "2"}]
    assert data["ports"] == "8000/http,22/tcp"
