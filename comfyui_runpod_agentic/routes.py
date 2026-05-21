from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import quote, urlencode

from .runpod_client import RunpodClientProtocol
from .state_store import StateStore

TERMINAL_PROXY_AUTH: dict[int, str] = {}


def route_payload(data: dict[str, Any], required: set[str]) -> dict[str, Any]:
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")
    return data


class RouteHandlers:
    def __init__(self, state_store: StateStore, runpod_client: RunpodClientProtocol):
        self.state_store = state_store
        self.runpod_client = runpod_client

    def resources(self) -> dict[str, Any]:
        local = self.state_store.list_resources()
        remote = [pod for pod in self.runpod_client.list_pods() if str(pod.get("name", "")).startswith("crag-")]
        return {"local": local, "remote": remote}

    def runs(self) -> dict[str, Any]:
        return {"runs": self.state_store.list_runs()}

    def run(self, run_id: str) -> dict[str, Any]:
        run = self.state_store.get_run(run_id)
        if not run:
            raise ValueError("Run not found.")
        return {"run": run}

    def stop_pod(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = route_payload(data, {"pod_id"})
        return {"pod": self.runpod_client.stop_pod(payload["pod_id"])}

    def resume_pod(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = route_payload(data, {"pod_id"})
        return {"pod": self.runpod_client.resume_pod(payload["pod_id"])}

    def terminate_pod(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = route_payload(data, {"pod_id"})
        self.runpod_client.terminate_pod(payload["pod_id"])
        return {"terminated": payload["pod_id"]}

    def cleanup(self, data: dict[str, Any]) -> dict[str, Any]:
        action = data.get("action", "stop")
        if action not in {"stop", "terminate"}:
            raise ValueError("cleanup action must be stop or terminate.")
        run_id = data.get("run_id")
        stale_seconds = int(data.get("stale_seconds") or 0)
        pod_ids = self._cleanup_pod_ids(run_id)
        affected = []
        for pod in self.runpod_client.list_pods():
            if not str(pod.get("name", "")).startswith("crag-"):
                continue
            if pod_ids is not None and pod.get("id") not in pod_ids:
                continue
            if stale_seconds and int((pod.get("runtime") or {}).get("uptimeInSeconds") or 0) < stale_seconds:
                continue
            if action == "terminate":
                self.runpod_client.terminate_pod(pod["id"])
            else:
                self.runpod_client.stop_pod(pod["id"])
            affected.append(pod["id"])
        return {"action": action, "affected": affected, "run_id": run_id, "stale_seconds": stale_seconds}

    def _cleanup_pod_ids(self, run_id: str | None) -> set[str] | None:
        if not run_id:
            return None
        return {
            resource["runpod_pod_id"]
            for resource in self.state_store.list_resources()
            if resource.get("run_id") == run_id and resource.get("runpod_pod_id")
        }

    def turn(self, run_id: str) -> dict[str, Any]:
        value = self.state_store.increment_counter(run_id, "turns")
        return {"run_id": run_id, "turns": value}


def register_routes(server: Any, handlers: RouteHandlers) -> None:
    try:
        routes = server.PromptServer.instance.routes
    except AttributeError:
        return

    @routes.get("/runpod-agentic/resources")
    async def resources(_request):
        return _json_response({"resources": handlers.resources()})

    @routes.get("/runpod-agentic/runs")
    async def runs(_request):
        return _json_response(handlers.runs())

    @routes.get("/runpod-agentic/runs/{run_id}")
    async def run(request):
        return _json_response(handlers.run(request.match_info["run_id"]))

    @routes.post("/runpod-agentic/pod/stop")
    async def stop(request):
        return _json_response(handlers.stop_pod(await request.json()))

    @routes.post("/runpod-agentic/pod/resume")
    async def resume(request):
        return _json_response(handlers.resume_pod(await request.json()))

    @routes.post("/runpod-agentic/pod/terminate")
    async def terminate(request):
        return _json_response(handlers.terminate_pod(await request.json()))

    @routes.post("/runpod-agentic/run/cleanup")
    async def cleanup(request):
        return _json_response(handlers.cleanup(await request.json()))

    @routes.post("/runpod-agentic/run/{run_id}/turn")
    async def turn(request):
        return _json_response(handlers.turn(request.match_info["run_id"]))

    @routes.route("*", "/runpod-agentic/terminal/{port}/{tail:.*}")
    async def terminal_proxy(request):
        return await _terminal_proxy(request)


def _json_response(payload: dict[str, Any]):
    from aiohttp import web

    return web.json_response(payload)


def terminal_proxy_path(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Terminal URL must use http or https.")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Only local terminal URLs can be proxied.")
    if parsed.port is None or parsed.port < 1 or parsed.port > 65535:
        raise ValueError("Terminal URL must include a valid port.")
    path = quote(parsed.path.lstrip("/"), safe="/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"/runpod-agentic/terminal/{parsed.port}/{path}{query}"


async def _terminal_proxy(request):
    from aiohttp import ClientError, ClientSession, web

    port = int(request.match_info["port"])
    if port < 1 or port > 65535:
        raise web.HTTPBadRequest(text="Invalid terminal port.")
    tail = request.match_info.get("tail", "")
    query = request.query.copy()
    auth = query.pop("__crag_terminal_auth", None)
    if auth:
        TERMINAL_PROXY_AUTH[port] = auth
    else:
        auth = TERMINAL_PROXY_AUTH.get(port)
    query_string = urlencode(list(query.items()))
    target = f"http://127.0.0.1:{port}/{tail}"
    if query_string:
        target = f"{target}?{query_string}"

    if request.headers.get("upgrade", "").lower() == "websocket":
        return await _terminal_websocket_proxy(request, target, auth)

    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length", "connection", "upgrade"}}
    if auth:
        headers["Authorization"] = terminal_authorization_header(auth)
    try:
        async with ClientSession() as session:
            async with session.request(request.method, target, headers=headers, data=await request.read(), allow_redirects=False) as response:
                body = await response.read()
                proxy_headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection", "content-encoding"}}
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    body = _inject_terminal_base(body, f"/runpod-agentic/terminal/{port}/")
                    proxy_headers["content-length"] = str(len(body))
                return web.Response(status=response.status, headers=proxy_headers, body=body)
    except ClientError as exc:
        raise web.HTTPBadGateway(text=f"Terminal on localhost:{port} is not reachable.") from exc


async def _terminal_websocket_proxy(request, target: str, auth: str | None):
    from aiohttp import ClientError, ClientSession, WSMsgType, web

    protocols = websocket_protocols(request.headers.get("Sec-WebSocket-Protocol"))
    ws_response = web.WebSocketResponse(protocols=protocols)
    await ws_response.prepare(request)
    target = "ws://" + target.removeprefix("http://")

    try:
        async with ClientSession() as session:
            headers = {"Authorization": terminal_authorization_header(auth)} if auth else None
            async with session.ws_connect(target, headers=headers, protocols=protocols) as upstream:
                async def client_to_upstream():
                    async for msg in ws_response:
                        if msg.type == WSMsgType.TEXT:
                            await upstream.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await upstream.send_bytes(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await upstream.close()

                async def upstream_to_client():
                    async for msg in upstream:
                        if msg.type == WSMsgType.TEXT:
                            await ws_response.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_response.send_bytes(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_response.close()

                await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)
    except ClientError:
        await ws_response.close(message=b"Terminal is not reachable.")
    return ws_response


def websocket_protocols(header: str | None) -> tuple[str, ...]:
    if not header:
        return ()
    return tuple(protocol.strip() for protocol in header.split(",") if protocol.strip())


def _inject_terminal_base(body: bytes, base_path: str) -> bytes:
    marker = b"<head>"
    if marker not in body:
        return body
    return body.replace(marker, marker + f'<base href="{base_path}">'.encode(), 1)


def terminal_authorization_header(auth: str) -> str:
    base64.b64decode(auth, validate=True)
    return f"Basic {auth}"
