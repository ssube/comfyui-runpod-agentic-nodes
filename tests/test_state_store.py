from types import SimpleNamespace

from comfyui_runpod_agentic.state_store import StateStore


def test_state_store_records_run_resource_event_and_counter(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    resource = SimpleNamespace(role="agent", desired_hash="abc", node_id="node1", template_id="template", name="pod", pod_input={})

    store.record_run("run1", "workflow", "deploy", "plan", "started")
    resource_id = store.record_resource("run1", resource, {"id": "pod1", "costPerHr": 1.25}, "RUNNING")
    store.add_event("run1", "test", "message", resource_id=resource_id)
    value = store.increment_counter("run1", "turns")

    assert store.get_run("run1")["status"] == "started"
    assert store.list_resources()[0]["runpod_pod_id"] == "pod1"
    assert value == 1
