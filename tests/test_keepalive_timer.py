from comfyui_runpod_agentic import keepalive_timer
from comfyui_runpod_agentic.runpod_client import RunpodClientError


class FakeProcess:
    pid = 12345


class FakeRunpodClient:
    def __init__(self):
        self.stopped = []
        self.terminated = []

    def stop_pod(self, pod_id):
        self.stopped.append(pod_id)
        return {"id": pod_id, "desiredStatus": "EXITED"}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)


def test_schedule_runpod_lifecycle_spawns_detached_timer(monkeypatch):
    spawned = []

    def fake_popen(command, **kwargs):
        spawned.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(keepalive_timer.subprocess, "Popen", fake_popen)

    result = keepalive_timer.schedule_runpod_lifecycle("pod-1", "stop", 7)

    assert result["pod_id"] == "pod-1"
    assert result["action"] == "stop"
    assert result["delay_seconds"] == 7
    assert result["pid"] == 12345
    assert spawned[0][0][-6:] == ["--pod-id", "pod-1", "--action", "stop", "--delay-seconds", "7"]
    assert spawned[0][1]["start_new_session"] is True


def test_apply_runpod_lifecycle_stops_pod(monkeypatch):
    client = FakeRunpodClient()
    monkeypatch.setattr(keepalive_timer, "RunpodClient", lambda: client)
    monkeypatch.setattr(keepalive_timer.time, "sleep", lambda _seconds: None)

    result = keepalive_timer.apply_runpod_lifecycle("pod-1", "stop", 5)

    assert result["result"] == {"id": "pod-1", "desiredStatus": "EXITED"}
    assert client.stopped == ["pod-1"]


def test_apply_runpod_lifecycle_terminates_pod(monkeypatch):
    client = FakeRunpodClient()
    monkeypatch.setattr(keepalive_timer, "RunpodClient", lambda: client)
    monkeypatch.setattr(keepalive_timer.time, "sleep", lambda _seconds: None)

    result = keepalive_timer.apply_runpod_lifecycle("pod-1", "terminate", 5)

    assert result == {"pod_id": "pod-1", "action": "terminate", "attempt": 1}
    assert client.terminated == ["pod-1"]


def test_apply_runpod_lifecycle_retries_transient_errors(monkeypatch):
    calls = []

    class FlakyClient:
        def stop_pod(self, pod_id):
            calls.append(pod_id)
            if len(calls) == 1:
                raise RunpodClientError("temporary")
            return {"id": pod_id, "desiredStatus": "EXITED"}

    monkeypatch.setattr(keepalive_timer, "RunpodClient", FlakyClient)
    monkeypatch.setattr(keepalive_timer.time, "sleep", lambda _seconds: None)

    result = keepalive_timer.apply_runpod_lifecycle("pod-1", "stop", 5)

    assert result["attempt"] == 2
    assert calls == ["pod-1", "pod-1"]


def test_apply_runpod_lifecycle_raises_after_retries(monkeypatch):
    class FailingClient:
        def stop_pod(self, _pod_id):
            raise RunpodClientError("still failing")

    monkeypatch.setattr(keepalive_timer, "RunpodClient", FailingClient)
    monkeypatch.setattr(keepalive_timer.time, "sleep", lambda _seconds: None)

    try:
        keepalive_timer.apply_runpod_lifecycle("pod-1", "stop", 5, attempts=2)
    except RuntimeError as exc:
        assert "still failing" in str(exc)
    else:
        raise AssertionError("Expected keep-alive lifecycle failure.")


def test_keepalive_timer_main(monkeypatch, capsys):
    monkeypatch.setattr(keepalive_timer, "apply_runpod_lifecycle", lambda pod_id, action, delay_seconds: {"pod_id": pod_id, "action": action, "delay_seconds": delay_seconds})
    monkeypatch.setattr(keepalive_timer.sys, "argv", ["keepalive_timer", "--pod-id", "pod-1", "--action", "stop", "--delay-seconds", "5"])

    assert keepalive_timer.main() == 0
    assert '"pod_id": "pod-1"' in capsys.readouterr().out
