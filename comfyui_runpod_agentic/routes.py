from __future__ import annotations

from typing import Any

from .runpod_client import RunpodClientProtocol
from .state_store import StateStore


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
        affected = []
        for pod in self.runpod_client.list_pods():
            if not str(pod.get("name", "")).startswith("crag-"):
                continue
            if action == "terminate":
                self.runpod_client.terminate_pod(pod["id"])
            else:
                self.runpod_client.stop_pod(pod["id"])
            affected.append(pod["id"])
        return {"action": action, "affected": affected}

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


def _json_response(payload: dict[str, Any]):
    from aiohttp import web

    return web.json_response(payload)
