import os
import time

import pytest

from comfyui_runpod_agentic.runpod_client import REQUIRED_GRAPHQL_TYPES, RunpodClient


def require_live_api() -> None:
    if os.environ.get("RUNPOD_LIVE_TESTS") != "1":
        pytest.skip("set RUNPOD_LIVE_TESTS=1 to run live Runpod tests")
    if not os.environ.get("RUNPOD_API_KEY"):
        pytest.skip("RUNPOD_API_KEY is required for live Runpod tests")


def test_live_graphql_schema_has_required_inputs():
    require_live_api()

    result = RunpodClient().validate_graphql_schema()

    assert set(REQUIRED_GRAPHQL_TYPES).issubset(result)
    assert all(item["present"] and not item["missing"] for item in result.values())


def test_live_create_test_template_pod_when_explicitly_enabled():
    require_live_api()
    if os.environ.get("RUNPOD_LIVE_CREATE_POD") != "1":
        pytest.skip("set RUNPOD_LIVE_CREATE_POD=1 to create a real test pod")
    template_id = os.environ.get("RUNPOD_TEST_TEMPLATE_ID")
    if not template_id:
        pytest.skip("RUNPOD_TEST_TEMPLATE_ID is required to create a real test pod")
    gpu_type_id = os.environ.get("RUNPOD_TEST_GPU_TYPE_ID")
    if not gpu_type_id:
        pytest.skip("RUNPOD_TEST_GPU_TYPE_ID is required to create a real test pod")

    client = RunpodClient()
    pod = client.create_or_deploy_pod(
        {
            "name": f"crag-pytest-{int(time.time())}",
            "templateId": template_id,
            "cloudType": os.environ.get("RUNPOD_TEST_CLOUD_TYPE", "COMMUNITY"),
            "gpuTypeId": gpu_type_id,
            "gpuCount": int(os.environ.get("RUNPOD_TEST_GPU_COUNT", "1")),
            "containerDiskInGb": int(os.environ.get("RUNPOD_TEST_CONTAINER_DISK_GB", "10")),
            "volumeInGb": 0,
            "ports": "22/tcp",
            "startSsh": True,
            "env": [],
        }
    )
    try:
        assert pod["id"]
    finally:
        client.terminate_pod(pod["id"])
