import json

import pytest

from comfyui_runpod_agentic import cleanup, schema_check
from comfyui_runpod_agentic.routes import RouteHandlers, register_routes, route_payload


class FakeRouteStore:
    def __init__(self):
        self.turns = 0

    def list_resources(self):
        return [{"id": "local"}]

    def list_runs(self):
        return [{"id": "run-1"}]

    def get_run(self, run_id):
        return {"id": run_id} if run_id == "run-1" else None

    def increment_counter(self, run_id, name):
        assert run_id == "run-1"
        assert name == "turns"
        self.turns += 1
        return self.turns


class FakeRouteClient:
    def __init__(self):
        self.stopped = []
        self.resumed = []
        self.terminated = []

    def list_pods(self):
        return [{"id": "pod-1", "name": "crag-agent"}, {"id": "pod-2", "name": "other"}]

    def stop_pod(self, pod_id):
        self.stopped.append(pod_id)
        return {"id": pod_id, "desiredStatus": "EXITED"}

    def resume_pod(self, pod_id):
        self.resumed.append(pod_id)
        return {"id": pod_id, "desiredStatus": "RUNNING"}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)


def test_route_payload_requires_fields():
    with pytest.raises(ValueError, match="pod_id"):
        route_payload({}, {"pod_id"})


def test_route_handlers_cover_resource_lifecycle():
    client = FakeRouteClient()
    handlers = RouteHandlers(FakeRouteStore(), client)

    assert handlers.resources()["remote"] == [{"id": "pod-1", "name": "crag-agent"}]
    assert handlers.runs() == {"runs": [{"id": "run-1"}]}
    assert handlers.run("run-1") == {"run": {"id": "run-1"}}
    assert handlers.stop_pod({"pod_id": "pod-1"})["pod"]["desiredStatus"] == "EXITED"
    assert handlers.resume_pod({"pod_id": "pod-1"})["pod"]["desiredStatus"] == "RUNNING"
    assert handlers.terminate_pod({"pod_id": "pod-1"}) == {"terminated": "pod-1"}
    assert handlers.cleanup({"action": "stop"}) == {"action": "stop", "affected": ["pod-1"]}
    assert handlers.cleanup({"action": "terminate"}) == {"action": "terminate", "affected": ["pod-1"]}
    assert handlers.turn("run-1") == {"run_id": "run-1", "turns": 1}
    assert client.stopped == ["pod-1", "pod-1"]
    assert client.resumed == ["pod-1"]
    assert client.terminated == ["pod-1", "pod-1"]


def test_route_handlers_report_missing_run_and_invalid_cleanup_action():
    handlers = RouteHandlers(FakeRouteStore(), FakeRouteClient())

    with pytest.raises(ValueError, match="Run not found"):
        handlers.run("missing")
    with pytest.raises(ValueError, match="cleanup action"):
        handlers.cleanup({"action": "reboot"})


def test_register_routes_noops_without_prompt_server():
    register_routes(object(), RouteHandlers(FakeRouteStore(), FakeRouteClient()))


def test_cleanup_managed_pods_stops_or_terminates_matching_pods(monkeypatch):
    client = FakeRouteClient()
    monkeypatch.setattr(cleanup, "RunpodClient", lambda: client)

    stopped = cleanup.cleanup_managed_pods("stop", "crag-")
    terminated = cleanup.cleanup_managed_pods("terminate", "crag-")

    assert stopped == [{"id": "pod-1", "name": "crag-agent", "action": "stop"}]
    assert terminated == [{"id": "pod-1", "name": "crag-agent", "action": "terminate"}]
    assert client.stopped == ["pod-1"]
    assert client.terminated == ["pod-1"]


def test_cleanup_main_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(cleanup, "cleanup_managed_pods", lambda action, prefix: [{"id": "pod-1", "action": action, "prefix": prefix}])
    monkeypatch.setattr("sys.argv", ["cleanup", "--action", "stop", "--prefix", "crag-"])

    assert cleanup.main() == 0

    assert json.loads(capsys.readouterr().out)["affected"] == [{"id": "pod-1", "action": "stop", "prefix": "crag-"}]


class FakeSchemaClient:
    def __init__(self, result):
        self.result = result

    def validate_graphql_schema(self):
        return self.result


def test_schema_check_main_reports_success_and_json(monkeypatch, capsys):
    result = {"PodStopInput": {"present": True, "fields": ["podId"], "missing": []}}
    monkeypatch.setattr(schema_check, "RunpodClient", lambda: FakeSchemaClient(result))
    monkeypatch.setattr("sys.argv", ["schema-check", "--json"])

    assert schema_check.main() == 0

    assert json.loads(capsys.readouterr().out) == result


def test_schema_check_main_returns_failure_for_missing_fields(monkeypatch, capsys):
    result = {"PodStopInput": {"present": True, "fields": [], "missing": ["podId"]}}
    monkeypatch.setattr(schema_check, "RunpodClient", lambda: FakeSchemaClient(result))
    monkeypatch.setattr("sys.argv", ["schema-check"])

    assert schema_check.main() == 1

    assert "PodStopInput: missing missing=podId" in capsys.readouterr().out
