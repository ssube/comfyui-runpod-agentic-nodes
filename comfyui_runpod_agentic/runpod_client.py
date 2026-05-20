from __future__ import annotations

import json
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import get_runpod_api_key

RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"
RUNPOD_REST_URL = "https://rest.runpod.io/v1"

REQUIRED_GRAPHQL_TYPES: dict[str, list[str]] = {
    "PodFindAndDeployOnDemandInput": [
        "cloudType",
        "containerDiskInGb",
        "env",
        "gpuCount",
        "gpuTypeId",
        "name",
        "ports",
        "startSsh",
        "templateId",
    ],
    "PodResumeInput": ["podId"],
    "SaveTemplateInput": ["containerDiskInGb", "dockerArgs", "env", "id", "imageName", "name", "ports", "volumeInGb", "volumeMountPath"],
    "PodStopInput": ["podId"],
    "PodTerminateInput": ["podId"],
}


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
    def create_network_volume(self, input: dict[str, Any]) -> dict[str, Any]: ...
    def delete_network_volume(self, volume_id: str) -> None: ...
    def list_gpu_types(self) -> list[dict[str, Any]]: ...
    def list_datacenters(self) -> list[dict[str, Any]]: ...
    def validate_graphql_schema(self) -> dict[str, Any]: ...


@dataclass
class RunpodClient:
    api_key: str | None = None
    endpoint: str = RUNPOD_GRAPHQL_URL
    rest_endpoint: str = RUNPOD_REST_URL
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
        data = self._graphql(mutation, {"input": normalize_pod_input(clean_none(input))})
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

    def save_template_rest(self, input: dict[str, Any]) -> dict[str, Any]:
        payload = normalize_template_rest_input(clean_none(input))
        template_id = payload.pop("id", None)
        if template_id:
            return self._rest_json("POST", f"/templates/{urllib.parse.quote(str(template_id))}/update", payload)
        return self._rest_json("POST", "/templates", payload)

    def create_network_volume(self, input: dict[str, Any]) -> dict[str, Any]:
        return self._rest_json("POST", "/networkvolumes", clean_none(input))

    def delete_network_volume(self, volume_id: str) -> None:
        self._rest_json("DELETE", f"/networkvolumes/{urllib.parse.quote(str(volume_id))}", None)

    def list_gpu_types(self) -> list[dict[str, Any]]:
        query = """
        query CragGpuTypes {
          gpuTypes {
            id
            displayName
            memoryInGb
            secureCloud
            communityCloud
            nodeGroupDatacenters {
              id
              name
              location
              storageSupport
              listed
            }
          }
        }
        """
        return self._graphql(query, {})["gpuTypes"]

    def list_datacenters(self) -> list[dict[str, Any]]:
        query = """
        query CragDatacenters {
          myself {
            datacenters {
              id
              name
              location
              storageSupport
              listed
            }
          }
        }
        """
        return self._graphql(query, {})["myself"]["datacenters"]

    def validate_graphql_schema(self) -> dict[str, Any]:
        query = """
        query CragSchemaCheck($typeName: String!) {
          __type(name: $typeName) {
            name
            inputFields { name }
          }
        }
        """
        result: dict[str, Any] = {}
        for type_name, required_fields in REQUIRED_GRAPHQL_TYPES.items():
            type_data = self._graphql(query, {"typeName": type_name}).get("__type")
            fields = sorted(field["name"] for field in (type_data or {}).get("inputFields", []))
            missing = sorted(set(required_fields).difference(fields))
            result[type_name] = {"present": type_data is not None, "fields": fields, "missing": missing}
        return result

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RunpodClientError("RUNPOD_API_KEY is required for Runpod API calls.")
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            endpoint_with_api_key(self.endpoint, self.api_key),
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "comfyui-runpod-agentic/0.1 (+https://github.com/ssube/runpod-sandbox-nodes)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = f"{exc.code} {exc.reason}"
            if body:
                detail = f"{detail}: {body}"
            raise RunpodClientError(f"Runpod API request failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RunpodClientError(f"Runpod API request failed: {exc}") from exc
        if payload.get("errors"):
            raise RunpodClientError(format_graphql_errors(payload["errors"]))
        return payload.get("data") or {}

    def _rest_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise RunpodClientError("RUNPOD_API_KEY is required for Runpod API calls.")
        url = self.rest_endpoint.rstrip("/") + "/" + path.lstrip("/")
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "comfyui-runpod-agentic/0.1 (+https://github.com/ssube/runpod-sandbox-nodes)",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            detail = f"{exc.code} {exc.reason}"
            if body_text:
                detail = f"{detail}: {body_text}"
            raise RunpodClientError(f"Runpod REST request failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RunpodClientError(f"Runpod REST request failed: {exc}") from exc


def clean_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [clean_none(item) for item in value]
    return value


def normalize_pod_input(input: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(input)
    env = normalized.get("env")
    if isinstance(env, dict):
        normalized["env"] = [{"key": key, "value": value} for key, value in sorted(env.items())]
    ports = normalized.get("ports")
    if ports == []:
        normalized.pop("ports", None)
    elif isinstance(ports, list):
        normalized["ports"] = ",".join(f"{port['container_port']}/{port.get('protocol', 'http')}" for port in ports)
    return normalized


def normalize_template_rest_input(input: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(input)
    env = normalized.get("env")
    if isinstance(env, list):
        normalized["env"] = {str(item["key"]): str(item["value"]) for item in env if isinstance(item, dict) and "key" in item and "value" in item}
    elif env is None:
        normalized["env"] = {}
    ports = normalized.get("ports")
    if isinstance(ports, str):
        normalized["ports"] = [part.strip() for part in ports.split(",") if part.strip()]
    docker_args = normalized.pop("dockerArgs", None)
    if docker_args and "dockerStartCmd" not in normalized:
        normalized["dockerStartCmd"] = shlex.split(str(docker_args))
    normalized.setdefault("isPublic", False)
    normalized.setdefault("isServerless", False)
    return normalized


def endpoint_with_api_key(endpoint: str, api_key: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "api_key"]
    query.append(("api_key", api_key))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def format_graphql_errors(errors: list[dict[str, Any]]) -> str:
    details = []
    for error in errors:
        path = ".".join(str(part) for part in error.get("path", [])) or "<unknown>"
        code = (error.get("extensions") or {}).get("code") or "UNKNOWN"
        details.append({"code": code, "message": error.get("message", ""), "path": path})
    return json.dumps(details, sort_keys=True)
