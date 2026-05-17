from comfyui_runpod_agentic.runpod_client import clean_none, endpoint_with_api_key
from comfyui_runpod_agentic.ssh_client import extract_ssh_endpoint


def test_clean_none_removes_nested_none_values():
    assert clean_none({"a": 1, "b": None, "c": {"d": None, "e": 2}}) == {"a": 1, "c": {"e": 2}}


def test_extract_ssh_endpoint_from_runtime_ports():
    pod = {"runtime": {"ports": [{"ip": "1.2.3.4", "privatePort": 22, "publicPort": 22022, "type": "tcp"}]}}

    assert extract_ssh_endpoint(pod) == ("1.2.3.4", 22022)


def test_endpoint_with_api_key_uses_query_parameter():
    url = endpoint_with_api_key("https://api.runpod.io/graphql?x=1", "token")

    assert url == "https://api.runpod.io/graphql?x=1&api_key=token"
