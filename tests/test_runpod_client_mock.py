import io
import json
import subprocess
import urllib.error

import pytest

from comfyui_runpod_agentic.runpod_client import (
    REQUIRED_GRAPHQL_TYPES,
    RunpodClient,
    RunpodClientError,
    clean_none,
    endpoint_with_api_key,
    format_graphql_errors,
    normalize_pod_input,
    normalize_pod_rest_input,
    normalize_template_rest_input,
)
from comfyui_runpod_agentic.ssh_client import (
    CommandResult,
    SSHConfig,
    SSHError,
    SubprocessSSHClient,
    extract_ssh_endpoint,
    normalize_ssh_result,
    runpod_proxy_ssh_endpoint,
)


def test_clean_none_removes_nested_none_values():
    assert clean_none({"a": 1, "b": None, "c": {"d": None, "e": 2}}) == {"a": 1, "c": {"e": 2}}


def test_extract_ssh_endpoint_from_runtime_ports():
    pod = {"runtime": {"ports": [{"ip": "1.2.3.4", "privatePort": 22, "publicPort": 22022, "type": "tcp"}]}}

    assert extract_ssh_endpoint(pod) == ("1.2.3.4", 22022)


def test_runpod_proxy_ssh_endpoint_uses_pod_id():
    assert runpod_proxy_ssh_endpoint({"id": "pod123"}, "abc") == ("pod123-abc@ssh.runpod.io", 22)


def test_runpod_proxy_ssh_endpoint_requires_id_and_suffix():
    with pytest.raises(SSHError, match="pod id"):
        runpod_proxy_ssh_endpoint({}, "abc")
    with pytest.raises(SSHError, match="proxy_key_suffix"):
        runpod_proxy_ssh_endpoint({"id": "pod123"}, "")


def test_extract_ssh_endpoint_rejects_missing_mapping():
    with pytest.raises(SSHError, match="SSH port 22"):
        extract_ssh_endpoint({"runtime": {"ports": [{"privatePort": 3000, "publicPort": 3000, "type": "http"}]}})


def test_normalize_ssh_result_rejects_runpod_pty_error():
    result = normalize_ssh_result(CommandResult(0, "Error: Your SSH client doesn't support PTY", ""))

    assert result.exit_code == 255


def test_normalize_ssh_result_rejects_container_not_found():
    result = normalize_ssh_result(CommandResult(0, "container not found", ""))

    assert result.exit_code == 255


def test_endpoint_with_api_key_uses_query_parameter():
    url = endpoint_with_api_key("https://api.runpod.io/graphql?x=1", "token")

    assert url == "https://api.runpod.io/graphql?x=1&api_key=token"


def test_graphql_http_error_includes_response_body(monkeypatch):
    def raise_http_error(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.runpod.io/graphql",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"errors":[{"message":"bad query"}]}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)

    with pytest.raises(RunpodClientError, match="bad query"):
        RunpodClient(api_key="token")._graphql("query Bad", {})


def test_graphql_raises_for_url_error(monkeypatch):
    def raise_url_error(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)

    with pytest.raises(RunpodClientError, match="offline"):
        RunpodClient(api_key="token")._graphql("query Bad", {})


def test_graphql_raises_for_graphql_errors(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"errors":[{"message":"bad field","path":["query"],"extensions":{"code":"BAD_USER_INPUT"}}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RunpodClientError, match="BAD_USER_INPUT"):
        RunpodClient(api_key="token")._graphql("query Bad", {})


def test_runpod_client_methods_parse_graphql_payloads(monkeypatch):
    seen = []

    def fake_graphql(self, query, variables):
        seen.append((query, variables))
        if "podFindAndDeployOnDemand" in query:
            return {"podFindAndDeployOnDemand": {"id": "pod-1"}}
        if "query Pods" in query:
            return {"myself": {"pods": [{"id": "pod-1"}]}}
        if "query Pod" in query:
            return {"pod": {"id": variables["input"]}}
        if "podStop" in query:
            return {"podStop": {"id": variables["input"]["podId"], "desiredStatus": "EXITED"}}
        if "podResume" in query:
            return {"podResume": {"id": variables["input"]["podId"], "desiredStatus": "RUNNING"}}
        if "saveTemplate" in query:
            return {"saveTemplate": {"id": "tpl-1"}}
        return {"podTerminate": True}

    monkeypatch.setattr(RunpodClient, "_graphql", fake_graphql)
    client = RunpodClient(api_key="token")

    assert client.create_or_deploy_pod({"name": "pod", "ports": []}) == {"id": "pod-1"}
    assert client.get_pod("pod-1") == {"id": "pod-1"}
    assert client.list_pods() == [{"id": "pod-1"}]
    assert client.stop_pod("pod-1")["desiredStatus"] == "EXITED"
    assert client.resume_pod("pod-1")["desiredStatus"] == "RUNNING"
    assert client.terminate_pod("pod-1") is None
    assert client.save_template({"name": "tpl"}) == {"id": "tpl-1"}
    assert "ports" not in seen[0][1]["input"]


def test_runpod_client_cpu_pods_use_rest_api(monkeypatch):
    calls = []

    def fake_rest(self, method, path, payload):
        calls.append((method, path, payload))
        return {"id": "cpu-pod", "desiredStatus": "RUNNING"}

    monkeypatch.setattr(RunpodClient, "_rest_json", fake_rest)
    client = RunpodClient(api_key="token", rest_endpoint="https://rest.example/v1")

    pod = client.create_or_deploy_pod({"name": "cpu-pod", "computeType": "CPU", "minVcpuCount": 2, "gpuTypeId": "CPU", "gpuCount": 0, "ports": [{"container_port": 22, "protocol": "tcp"}], "dockerArgs": "sleep infinity", "startSsh": True, "stopAfter": "2026-05-22T00:00:00Z"})

    assert pod["id"] == "cpu-pod"
    assert calls == [("POST", "/pods", {"name": "cpu-pod", "computeType": "CPU", "vcpuCount": 2, "ports": ["22/tcp"], "dockerStartCmd": ["sleep", "infinity"]})]


def test_normalize_pod_input_preserves_cpu_fields():
    normalized = normalize_pod_input(
        {
            "computeType": "CPU",
            "minVcpuCount": 2,
            "ports": [{"container_port": 22, "protocol": "tcp"}],
        }
    )

    assert normalized == {"computeType": "CPU", "minVcpuCount": 2, "ports": "22/tcp"}


def test_normalize_pod_rest_input_converts_cpu_fields():
    normalized = normalize_pod_rest_input({"computeType": "CPU", "minVcpuCount": 2, "gpuTypeId": "CPU", "gpuCount": 0, "ports": [{"container_port": 22, "protocol": "tcp"}], "dockerArgs": "sleep infinity", "startSsh": True, "stopAfter": "2026-05-22T00:00:00Z"})

    assert normalized == {"computeType": "CPU", "vcpuCount": 2, "ports": ["22/tcp"], "dockerStartCmd": ["sleep", "infinity"]}


def test_normalize_pod_rest_input_converts_env_ports_and_gpu_fields():
    normalized = normalize_pod_rest_input(
        {
            "env": [{"key": "A", "value": "B"}],
            "ports": "22/tcp,7681/http",
            "gpuTypeId": "NVIDIA RTX A4000",
            "dockerArgs": "ignored",
            "dockerStartCmd": ["sleep", "infinity"],
        }
    )

    assert normalized == {"env": {"A": "B"}, "ports": ["22/tcp", "7681/http"], "gpuTypeIds": ["NVIDIA RTX A4000"], "dockerStartCmd": ["sleep", "infinity"]}


def test_normalize_pod_rest_input_preserves_cpu_vcpu_count():
    normalized = normalize_pod_rest_input({"computeType": "CPU", "minVcpuCount": 2, "vcpuCount": 4})

    assert normalized == {"computeType": "CPU", "vcpuCount": 4}


def test_runpod_client_lists_runtime_dropdown_options(monkeypatch):
    def fake_graphql(self, query, _variables):
        if "gpuTypes" in query:
            return {"gpuTypes": [{"id": "NVIDIA RTX A4000", "displayName": "A4000"}]}
        return {"myself": {"datacenters": [{"id": "US-KS-2", "listed": True}]}}

    monkeypatch.setattr(RunpodClient, "_graphql", fake_graphql)
    client = RunpodClient(api_key="token")

    assert client.list_gpu_types() == [{"id": "NVIDIA RTX A4000", "displayName": "A4000"}]
    assert client.list_datacenters() == [{"id": "US-KS-2", "listed": True}]


def test_runpod_client_rest_template_create_update_and_errors(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"id":"tpl-1"}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = RunpodClient(api_key="token", rest_endpoint="https://rest.example/v1")

    assert client.save_template_rest({"name": "new"}) == {"id": "tpl-1"}
    assert client.save_template_rest({"id": "tpl old", "name": "existing"}) == {"id": "tpl-1"}
    assert requests[0][0].full_url == "https://rest.example/v1/templates"
    assert requests[1][0].full_url == "https://rest.example/v1/templates/tpl%20old/update"

    def raise_http_error(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://rest.example/v1/templates", 500, "Server Error", {}, io.BytesIO(b"bad rest"))

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)
    with pytest.raises(RunpodClientError, match="bad rest"):
        client.save_template_rest({"name": "bad"})


def test_validate_graphql_schema_reports_missing_fields(monkeypatch):
    def fake_graphql(self, _query, variables):
        type_name = variables["typeName"]
        if type_name == "PodStopInput":
            return {"__type": None}
        return {"__type": {"inputFields": [{"name": field} for field in REQUIRED_GRAPHQL_TYPES[type_name][:-1]]}}

    monkeypatch.setattr(RunpodClient, "_graphql", fake_graphql)

    result = RunpodClient(api_key="token").validate_graphql_schema()

    assert result["PodStopInput"]["present"] is False
    assert "podId" in result["PodStopInput"]["missing"]
    assert result["PodFindAndDeployOnDemandInput"]["missing"]


def test_subprocess_ssh_client_builds_direct_and_proxy_commands(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return type("Proc", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = SubprocessSSHClient(SSHConfig(username="ubuntu", private_key_path="/tmp/key", command_timeout_seconds=7))

    assert client.run("1.2.3.4", 2222, "true").stdout == "ok"
    assert client.run("pod-abc@ssh.runpod.io", 22, "true").stdout == "ok"
    client.write_file("1.2.3.4", 2222, "/workspace/file.txt", "hello")

    assert calls[0][0][-1] == "true"
    assert calls[1][0][1] == "-tt"
    assert calls[1][1]["input"] == "true\nexit\n"
    assert calls[2][0][-1] == "mkdir -p '/workspace'"
    assert calls[3][1]["input"] == "hello"


def test_subprocess_ssh_client_raises_on_write_failure(monkeypatch):
    def fake_run(args, **_kwargs):
        return type("Proc", (), {"returncode": 1, "stdout": "", "stderr": json.dumps(args)})()

    monkeypatch.setattr("subprocess.run", fake_run)
    client = SubprocessSSHClient(SSHConfig(username="ubuntu", private_key_path="/tmp/key"))

    with pytest.raises(SSHError):
        client.write_file("1.2.3.4", 2222, "/workspace/file.txt", "hello")


def test_subprocess_ssh_client_returns_timeout_result(monkeypatch):
    def fake_run(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", fake_run)
    client = SubprocessSSHClient(SSHConfig(username="ubuntu", private_key_path="/tmp/key", command_timeout_seconds=7))

    result = client.run("pod-abc@ssh.runpod.io", 22, "true", timeout_seconds=3)

    assert result.exit_code == 124
    assert "timed out" in result.stderr


def test_format_graphql_errors_preserves_path_and_code():
    message = format_graphql_errors([{"message": "Something went wrong", "path": ["podFindAndDeployOnDemand"], "extensions": {"code": "INTERNAL_SERVER_ERROR"}}])

    assert "podFindAndDeployOnDemand" in message
    assert "INTERNAL_SERVER_ERROR" in message


def test_normalize_pod_input_converts_env_and_ports():
    data = normalize_pod_input(
        {
            "env": {"B": "2", "A": "1"},
            "ports": [{"container_port": 8000, "protocol": "http"}, {"container_port": 22, "protocol": "tcp"}],
        }
    )

    assert data["env"] == [{"key": "A", "value": "1"}, {"key": "B", "value": "2"}]
    assert data["ports"] == "8000/http,22/tcp"


def test_normalize_template_rest_input_converts_legacy_graphql_shape():
    data = normalize_template_rest_input(
        {
            "env": [{"key": "RUNPOD_TOKEN", "value": "secret"}],
            "ports": "22/tcp,3000/http",
            "dockerArgs": "sleep infinity",
        }
    )

    assert data["env"] == {"RUNPOD_TOKEN": "secret"}
    assert data["ports"] == ["22/tcp", "3000/http"]
    assert data["dockerStartCmd"] == ["sleep", "infinity"]
    assert data["isPublic"] is False
