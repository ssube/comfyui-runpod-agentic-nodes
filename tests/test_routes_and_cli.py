import asyncio
import json
import socket
from binascii import Error
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, WSMsgType, web

from comfyui_runpod_agentic import cleanup, schema_check
from comfyui_runpod_agentic.routes import (
    RouteHandlers,
    _decode_terminal_origin,
    _encode_terminal_origin,
    _inject_terminal_base,
    _json_response,
    _remote_terminal_proxy,
    _terminal_proxy,
    register_routes,
    route_payload,
    terminal_authorization_header,
    terminal_proxy_path,
    websocket_protocols,
)


class FakeRouteStore:
    def __init__(self):
        self.turns = 0

    def list_resources(self):
        return [{"id": "local", "run_id": "run-1", "runpod_pod_id": "pod-1"}, {"id": "other", "run_id": "run-2", "runpod_pod_id": "pod-3"}]

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
        return [
            {"id": "pod-1", "name": "crag-agent", "runtime": {"uptimeInSeconds": 3600}},
            {"id": "pod-3", "name": "crag-other", "runtime": {"uptimeInSeconds": 30}},
            {"id": "pod-2", "name": "other"},
        ]

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

    assert handlers.resources()["remote"] == [
        {"id": "pod-1", "name": "crag-agent", "runtime": {"uptimeInSeconds": 3600}},
        {"id": "pod-3", "name": "crag-other", "runtime": {"uptimeInSeconds": 30}},
    ]
    assert handlers.runs() == {"runs": [{"id": "run-1"}]}
    assert handlers.run("run-1") == {"run": {"id": "run-1"}}
    assert handlers.stop_pod({"pod_id": "pod-1"})["pod"]["desiredStatus"] == "EXITED"
    assert handlers.resume_pod({"pod_id": "pod-1"})["pod"]["desiredStatus"] == "RUNNING"
    assert handlers.terminate_pod({"pod_id": "pod-1"}) == {"terminated": "pod-1"}
    assert handlers.cleanup({"action": "stop", "run_id": "run-1"}) == {"action": "stop", "affected": ["pod-1"], "run_id": "run-1", "stale_seconds": 0}
    assert handlers.cleanup({"action": "terminate", "stale_seconds": 300}) == {"action": "terminate", "affected": ["pod-1"], "run_id": None, "stale_seconds": 300}
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


def test_register_routes_binds_expected_handlers():
    class FakeRoutes:
        def __init__(self):
            self.bound = []
            self.handlers = {}

        def get(self, path):
            return self._bind("GET", path)

        def post(self, path):
            return self._bind("POST", path)

        def route(self, method, path):
            return self._bind(method, path)

        def _bind(self, method, path):
            def decorator(func):
                self.bound.append((method, path, func.__name__))
                self.handlers[(method, path)] = func
                return func

            return decorator

    routes = FakeRoutes()
    server = SimpleNamespace(PromptServer=SimpleNamespace(instance=SimpleNamespace(routes=routes)))

    register_routes(server, RouteHandlers(FakeRouteStore(), FakeRouteClient()))

    assert ("GET", "/runpod-agentic/resources", "resources") in routes.bound
    assert ("POST", "/runpod-agentic/pod/terminate", "terminate") in routes.bound
    assert ("*", "/runpod-agentic/terminal/{port}/{tail:.*}", "terminal_proxy") in routes.bound

    async def exercise_handlers():
        resources = await routes.handlers[("GET", "/runpod-agentic/resources")](None)
        runs = await routes.handlers[("GET", "/runpod-agentic/runs")](None)
        run = await routes.handlers[("GET", "/runpod-agentic/runs/{run_id}")](SimpleNamespace(match_info={"run_id": "run-1"}))
        stop = await routes.handlers[("POST", "/runpod-agentic/pod/stop")](FakeJsonRequest({"pod_id": "pod-1"}))
        resume = await routes.handlers[("POST", "/runpod-agentic/pod/resume")](FakeJsonRequest({"pod_id": "pod-1"}))
        terminate = await routes.handlers[("POST", "/runpod-agentic/pod/terminate")](FakeJsonRequest({"pod_id": "pod-1"}))
        cleanup_response = await routes.handlers[("POST", "/runpod-agentic/run/cleanup")](FakeJsonRequest({"action": "stop", "run_id": "run-1"}))
        turn = await routes.handlers[("POST", "/runpod-agentic/run/{run_id}/turn")](SimpleNamespace(match_info={"run_id": "run-1"}))
        return resources, runs, run, stop, resume, terminate, cleanup_response, turn

    resources, runs, run, stop, resume, terminate, cleanup_response, turn = asyncio.run(exercise_handlers())

    assert resources.status == 200
    assert json.loads(runs.text)["runs"] == [{"id": "run-1"}]
    assert json.loads(run.text)["run"] == {"id": "run-1"}
    assert json.loads(stop.text)["pod"]["desiredStatus"] == "EXITED"
    assert json.loads(resume.text)["pod"]["desiredStatus"] == "RUNNING"
    assert json.loads(terminate.text) == {"terminated": "pod-1"}
    assert json.loads(cleanup_response.text)["affected"] == ["pod-1"]
    assert json.loads(turn.text)["turns"] == 1


def test_terminal_proxy_path_accepts_only_local_terminal_urls():
    assert terminal_proxy_path("http://127.0.0.1:7681/?arg=1") == "/runpod-agentic/terminal/7681/?arg=1"
    assert terminal_proxy_path("http://localhost:8765/term/ws") == "/runpod-agentic/terminal/8765/term/ws"
    remote_origin = _encode_terminal_origin("https://pod.runpod.net:17681")
    assert terminal_proxy_path("https://pod.runpod.net:17681/term?arg=1") == f"/runpod-agentic/terminal/remote/{remote_origin}/term?arg=1"

    with pytest.raises(ValueError, match="valid port"):
        terminal_proxy_path("http://127.0.0.1/")
    with pytest.raises(ValueError, match="http or https"):
        terminal_proxy_path("ws://127.0.0.1:7681/")


def test_terminal_authorization_header_validates_base64():
    assert terminal_authorization_header("Y3JhZzpzZWNyZXQ=") == "Basic Y3JhZzpzZWNyZXQ="

    with pytest.raises(Error):
        terminal_authorization_header("not base64")


def test_websocket_protocols_preserve_ttyd_subprotocol():
    assert websocket_protocols("tty") == ("tty",)
    assert websocket_protocols(" tty, other ") == ("tty", "other")
    assert websocket_protocols(None) == ()


def test_terminal_html_base_injection():
    assert _inject_terminal_base(b"<html><head><title>x</title></head></html>", "/proxy/") == b'<html><head><base href="/proxy/"><title>x</title></head></html>'
    assert _inject_terminal_base(b"<html><body>x</body></html>", "/proxy/") == b"<html><body>x</body></html>"


def test_terminal_origin_encoding_round_trips():
    encoded = _encode_terminal_origin("https://pod.runpod.net:17681")

    assert _decode_terminal_origin(encoded) == "https://pod.runpod.net:17681"

    with pytest.raises(ValueError, match="Invalid remote"):
        _decode_terminal_origin(_encode_terminal_origin("ws://pod.runpod.net"))


def test_json_response_uses_aiohttp_response():
    response = _json_response({"ok": True})

    assert response.status == 200
    assert json.loads(response.text) == {"ok": True}


def test_terminal_proxy_rewrites_html_and_forwards_auth():
    async def run_proxy():
        seen = {}

        async def handler(request):
            seen["authorization"] = request.headers.get("Authorization")
            return web.Response(text="<html><head><title>x</title></head></html>", content_type="text/html")

        runner = web.AppRunner(web.Application())
        runner.app.router.add_get("/term", handler)
        port = unused_port()
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        try:
            request = FakeProxyRequest(port, "term", {"__crag_terminal_auth": "Y3JhZzpzZWNyZXQ="})
            response = await _terminal_proxy(request)
            return response, seen
        finally:
            await runner.cleanup()

    response, seen = asyncio.run(run_proxy())

    assert response.status == 200
    assert b'<base href="/runpod-agentic/terminal/' in response.body
    assert seen["authorization"] == "Basic Y3JhZzpzZWNyZXQ="


def test_remote_terminal_proxy_rewrites_html_and_forwards_auth():
    async def run_proxy():
        seen = {}

        async def handler(request):
            seen["authorization"] = request.headers.get("Authorization")
            return web.Response(text="<html><head><title>x</title></head></html>", content_type="text/html")

        runner = web.AppRunner(web.Application())
        runner.app.router.add_get("/term", handler)
        port = unused_port()
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        try:
            origin = _encode_terminal_origin(f"http://127.0.0.1:{port}")
            request = FakeRemoteProxyRequest(origin, "term", {"__crag_terminal_auth": "Y3JhZzpzZWNyZXQ="})
            response = await _remote_terminal_proxy(request)
            return response, seen, origin
        finally:
            await runner.cleanup()

    response, seen, origin = asyncio.run(run_proxy())

    assert response.status == 200
    assert f'/runpod-agentic/terminal/remote/{origin}/'.encode() in response.body
    assert seen["authorization"] == "Basic Y3JhZzpzZWNyZXQ="


def test_terminal_proxy_forwards_websocket_messages_and_auth():
    async def run_proxy():
        seen = {}

        async def upstream_handler(request):
            seen["authorization"] = request.headers.get("Authorization")
            ws = web.WebSocketResponse(protocols=websocket_protocols(request.headers.get("Sec-WebSocket-Protocol")))
            await ws.prepare(request)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await ws.send_str(f"echo:{message.data}")
                    await ws.close()
            return ws

        upstream = web.AppRunner(web.Application())
        upstream.app.router.add_get("/term", upstream_handler)
        upstream_port = unused_port()
        await upstream.setup()
        upstream_site = web.TCPSite(upstream, "127.0.0.1", upstream_port)
        await upstream_site.start()

        proxy = web.AppRunner(web.Application())
        proxy.app.router.add_route("*", "/terminal/{port}/{tail:.*}", _terminal_proxy)
        proxy_port = unused_port()
        await proxy.setup()
        proxy_site = web.TCPSite(proxy, "127.0.0.1", proxy_port)
        await proxy_site.start()
        try:
            async with ClientSession() as session:
                url = f"http://127.0.0.1:{proxy_port}/terminal/{upstream_port}/term?__crag_terminal_auth=Y3JhZzpzZWNyZXQ="
                async with session.ws_connect(url, protocols=("tty",)) as ws:
                    await ws.send_str("ping")
                    response = await ws.receive()
                    return response.data, seen
        finally:
            await proxy.cleanup()
            await upstream.cleanup()

    response, seen = asyncio.run(run_proxy())

    assert response == "echo:ping"
    assert seen["authorization"] == "Basic Y3JhZzpzZWNyZXQ="


def test_remote_terminal_proxy_forwards_websocket_messages_and_auth():
    async def run_proxy():
        seen = {}

        async def upstream_handler(request):
            seen["authorization"] = request.headers.get("Authorization")
            ws = web.WebSocketResponse(protocols=websocket_protocols(request.headers.get("Sec-WebSocket-Protocol")))
            await ws.prepare(request)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await ws.send_str(f"remote:{message.data}")
                    await ws.close()
            return ws

        upstream = web.AppRunner(web.Application())
        upstream.app.router.add_get("/term", upstream_handler)
        upstream_port = unused_port()
        await upstream.setup()
        upstream_site = web.TCPSite(upstream, "127.0.0.1", upstream_port)
        await upstream_site.start()

        proxy = web.AppRunner(web.Application())
        proxy.app.router.add_route("*", "/terminal/remote/{origin}/{tail:.*}", _remote_terminal_proxy)
        proxy_port = unused_port()
        await proxy.setup()
        proxy_site = web.TCPSite(proxy, "127.0.0.1", proxy_port)
        await proxy_site.start()
        try:
            origin = _encode_terminal_origin(f"http://127.0.0.1:{upstream_port}")
            async with ClientSession() as session:
                url = f"http://127.0.0.1:{proxy_port}/terminal/remote/{origin}/term?__crag_terminal_auth=Y3JhZzpzZWNyZXQ="
                async with session.ws_connect(url, protocols=("tty",)) as ws:
                    await ws.send_str("ping")
                    response = await ws.receive()
                    return response.data, seen
        finally:
            await proxy.cleanup()
            await upstream.cleanup()

    response, seen = asyncio.run(run_proxy())

    assert response == "remote:ping"
    assert seen["authorization"] == "Basic Y3JhZzpzZWNyZXQ="


def test_terminal_proxy_rejects_invalid_port():
    request = FakeProxyRequest(70000, "", {})

    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(_terminal_proxy(request))


class FakeJsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeProxyRequest:
    def __init__(self, port, tail, query):
        self.match_info = {"port": str(port), "tail": tail}
        self.query = dict(query)
        self.headers = {}
        self.method = "GET"

    async def read(self):
        return b""


class FakeRemoteProxyRequest(FakeProxyRequest):
    def __init__(self, origin, tail, query, headers=None):
        super().__init__(7681, tail, query)
        self.headers = headers or {}
        self.match_info = {"origin": origin, "tail": tail}


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    assert websocket_protocols(None) == ()


def test_cleanup_managed_pods_stops_or_terminates_matching_pods(monkeypatch):
    client = FakeRouteClient()
    monkeypatch.setattr(cleanup, "RunpodClient", lambda: client)

    stopped = cleanup.cleanup_managed_pods("stop", "crag-")
    terminated = cleanup.cleanup_managed_pods("terminate", "crag-")

    assert stopped == [{"id": "pod-1", "name": "crag-agent", "action": "stop"}, {"id": "pod-3", "name": "crag-other", "action": "stop"}]
    assert terminated == [{"id": "pod-1", "name": "crag-agent", "action": "terminate"}, {"id": "pod-3", "name": "crag-other", "action": "terminate"}]
    assert client.stopped == ["pod-1", "pod-3"]
    assert client.terminated == ["pod-1", "pod-3"]


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
