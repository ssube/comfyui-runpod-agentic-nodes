import os
import time

from comfyui_runpod_agentic.live_smoke import build_smoke_deployment
from comfyui_runpod_agentic.planner import Planner
from comfyui_runpod_agentic.runpod_client import REQUIRED_GRAPHQL_TYPES, RunpodClient


class FakeLiveRunpodClient:
    def __init__(self):
        self.terminated = []

    def validate_graphql_schema(self):
        return {name: {"present": True, "fields": fields, "missing": []} for name, fields in REQUIRED_GRAPHQL_TYPES.items()}

    def create_or_deploy_pod(self, input):
        return {"id": "pod-offline-live-test", "input": input}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)


def live_client():
    if os.environ.get("RUNPOD_LIVE_TESTS") == "1" and os.environ.get("RUNPOD_API_KEY"):
        return RunpodClient()
    return FakeLiveRunpodClient()


def test_live_graphql_schema_has_required_inputs():
    result = live_client().validate_graphql_schema()

    assert set(REQUIRED_GRAPHQL_TYPES).issubset(result)
    assert all(item["present"] and not item["missing"] for item in result.values())


def test_live_create_test_template_pod_when_explicitly_enabled():
    live_create = os.environ.get("RUNPOD_LIVE_TESTS") == "1" and os.environ.get("RUNPOD_LIVE_CREATE_POD") == "1"
    template_id = os.environ.get("RUNPOD_TEST_TEMPLATE_ID") if live_create else "template-offline-live-test"
    gpu_type_id = os.environ.get("RUNPOD_TEST_GPU_TYPE_ID") if live_create else "gpu-offline-live-test"
    client = live_client()
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


def test_live_smoke_defaults_to_internal_sshd():
    deployment = build_smoke_deployment("NVIDIA RTX A4500", 1, 15, "SECURE")
    plan = Planner().build(deployment, mode="plan", workflow_graph={"live_smoke": True})
    agent = next(resource for resource in plan.resources if resource.role == "agent")

    assert plan.ssh_access.mode == "internal_sshd"
    assert plan.ssh_access.install_internal_sshd is True
    if "RUNPOD_SSH_PUBLIC_KEY" in agent.pod_input["env"]:
        assert agent.pod_input["env"]["RUNPOD_SSH_PUBLIC_KEY"].startswith("ssh-")
    assert "sshd" in agent.pod_input["dockerArgs"]


def test_live_smoke_can_still_request_runpod_proxy():
    deployment = build_smoke_deployment("NVIDIA RTX A4500", 1, 15, "SECURE", "runpod_proxy")
    plan = Planner().build(deployment, mode="plan", workflow_graph={"live_smoke": True})
    agent = next(resource for resource in plan.resources if resource.role == "agent")

    assert plan.ssh_access.mode == "runpod_proxy"
    assert plan.ssh_access.install_internal_sshd is False
    assert "dockerArgs" not in agent.pod_input
