from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import get_runpod_api_key

RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"


class RunpodClientError(RuntimeError):
    pass


class RunpodClientProtocol(Protocol):
    def create_or_deploy_pod(self, input: dict[str, Any]) -> dict[str, Any]: ...
    def get_pod(self, pod_id: str) -> dict[str, Any]: ...
    def list_pods(self) -> list[dict[str, Any]]: ...
    def stop_pod(self, pod_id: str) -> dict[str, Any]: ...
    def resume_pod(self, pod_id: str) -> dict[str, Any]: ...
    def terminate_pod(self, pod_id: str) -> None: ...
    def save_template(self, input: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class RunpodClient:
    api_key: str | None = None
    endpoint: str = RUNPOD_GRAPHQL_URL
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = get_runpod_api_key()

    def create_or_deploy_pod(self, input: dict[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation CreatePod($input: PodFindAndDeployOnDemandInput!) {
          podFindAndDeployOnDemand(input: $input) { id name desiredStatus runtime { uptimeInSeconds ports { ip isIpPublic privatePort publicPort type } } }
        }
        """
        data = self._graphql(mutation, {"input": clean_none(input)})
        return data["podFindAndDeployOnDemand"]

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        query = """
        query Pod($input: String!) {
          pod(input: { podId: $input }) { id name desiredStatus costPerHr adjustedCostPerHr runtime { ports { ip isIpPublic privatePort publicPort type } } }
        }
        """
        return self._graphql(query, {"input": pod_id})["pod"]

    def list_pods(self) -> list[dict[str, Any]]:
        query = """
        query Pods { myself { pods { id name desiredStatus costPerHr adjustedCostPerHr runtime { ports { ip isIpPublic privatePort publicPort type } } } } }
        """
        return self._graphql(query, {})["myself"]["pods"]

    def stop_pod(self, pod_id: str) -> dict[str, Any]:
        mutation = "mutation Stop($input: PodStopInput!) { podStop(input: $input) { id desiredStatus } }"
        return self._graphql(mutation, {"input": {"podId": pod_id}})["podStop"]

    def resume_pod(self, pod_id: str) -> dict[str, Any]:
        mutation = "mutation Resume($input: PodResumeInput!) { podResume(input: $input) { id desiredStatus } }"
        return self._graphql(mutation, {"input": {"podId": pod_id}})["podResume"]

    def terminate_pod(self, pod_id: str) -> None:
        mutation = "mutation Terminate($input: PodTerminateInput!) { podTerminate(input: $input) }"
        self._graphql(mutation, {"input": {"podId": pod_id}})

    def save_template(self, input: dict[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation SaveTemplate($input: SaveTemplateInput!) {
          saveTemplate(input: $input) {
            id
            name
            imageName
            containerDiskInGb
            volumeInGb
            volumeMountPath
            dockerArgs
            ports
            env { key value }
          }
        }
        """
        return self._graphql(mutation, {"input": clean_none(input)})["saveTemplate"]

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RunpodClientError("RUNPOD_API_KEY is required for Runpod API calls.")
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            endpoint_with_api_key(self.endpoint, self.api_key),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "comfyui-runpod-agentic/0.1 (+https://github.com/ssube/runpod-sandbox-nodes)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RunpodClientError(f"Runpod API request failed: {exc}") from exc
        if payload.get("errors"):
            raise RunpodClientError(json.dumps(payload["errors"], sort_keys=True))
        return payload.get("data") or {}


def clean_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [clean_none(item) for item in value]
    return value


def endpoint_with_api_key(endpoint: str, api_key: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "api_key"]
    query.append(("api_key", api_key))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
