from .nodes import (
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodBuildContainerNode,
    RunpodComposeYAMLNode,
    RunpodContainerdApplyNode,
    RunpodDockerComposeApplyNode,
    RunpodKeepAliveNode,
    RunpodLanguageRuntimeNode,
    RunpodLLMApiNode,
    RunpodLLMServerNode,
    RunpodLocalSQLDatabaseNode,
    RunpodLogsNode,
    RunpodMCPServerNode,
    RunpodNetworkStorageNode,
    RunpodPackageNode,
    RunpodPodmanComposeApplyNode,
    RunpodPodNode,
    RunpodRemoteSQLDatabaseNode,
    RunpodRunNode,
    RunpodS3StorageNode,
    RunpodSkillFrameworkNode,
    RunpodSkillNode,
    RunpodSSHAccessNode,
    RunpodSSHCommandNode,
    RunpodStartupScriptNode,
    RunpodVectorDatabaseNode,
)

NODE_CLASS_MAPPINGS = {
    "RunpodAgent": RunpodAgentNode,
    "RunpodBuildContainer": RunpodBuildContainerNode,
    "RunpodBrowser": RunpodBrowserNode,
    "RunpodLLMServer": RunpodLLMServerNode,
    "RunpodLLMApi": RunpodLLMApiNode,
    "RunpodMCPServer": RunpodMCPServerNode,
    "RunpodSkill": RunpodSkillNode,
    "RunpodSkillFramework": RunpodSkillFrameworkNode,
    "RunpodRemoteSQLDatabase": RunpodRemoteSQLDatabaseNode,
    "RunpodLocalSQLDatabase": RunpodLocalSQLDatabaseNode,
    "RunpodVectorDatabase": RunpodVectorDatabaseNode,
    "RunpodNetworkStorage": RunpodNetworkStorageNode,
    "RunpodPackage": RunpodPackageNode,
    "RunpodLanguageRuntime": RunpodLanguageRuntimeNode,
    "RunpodS3Storage": RunpodS3StorageNode,
    "RunpodSSHCommand": RunpodSSHCommandNode,
    "RunpodKeepAlive": RunpodKeepAliveNode,
    "RunpodSSHAccess": RunpodSSHAccessNode,
    "RunpodPod": RunpodPodNode,
    "RunpodRun": RunpodRunNode,
    "RunpodStartupScript": RunpodStartupScriptNode,
    "RunpodComposeYAML": RunpodComposeYAMLNode,
    "RunpodDockerComposeApply": RunpodDockerComposeApplyNode,
    "RunpodPodmanComposeApply": RunpodPodmanComposeApplyNode,
    "RunpodContainerdApply": RunpodContainerdApplyNode,
    "RunpodLogs": RunpodLogsNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RunpodAgent": "Agent",
    "RunpodBuildContainer": "Build Container",
    "RunpodBrowser": "Browser",
    "RunpodLLMServer": "LLM Server",
    "RunpodLLMApi": "LLM API",
    "RunpodMCPServer": "MCP Server",
    "RunpodSkill": "Skill",
    "RunpodSkillFramework": "Skill Framework",
    "RunpodRemoteSQLDatabase": "Remote SQL Database",
    "RunpodLocalSQLDatabase": "Local SQL Database",
    "RunpodVectorDatabase": "Vector Database",
    "RunpodNetworkStorage": "Network Storage",
    "RunpodPackage": "Package",
    "RunpodLanguageRuntime": "Language Runtime",
    "RunpodS3Storage": "S3 Storage",
    "RunpodSSHCommand": "SSH Command",
    "RunpodKeepAlive": "Keep Alive",
    "RunpodSSHAccess": "SSH Access",
    "RunpodPod": "Deploy",
    "RunpodRun": "Run on Runpod",
    "RunpodStartupScript": "Startup Script",
    "RunpodComposeYAML": "Compose YAML",
    "RunpodDockerComposeApply": "Docker Compose Apply",
    "RunpodPodmanComposeApply": "Podman Compose Apply",
    "RunpodContainerdApply": "Containerd Apply",
    "RunpodLogs": "Runpod Logs",
}

WEB_DIRECTORY = "./web"


def _try_register_routes() -> None:
    try:
        import server  # type: ignore

        from .routes import RouteHandlers, register_routes
        from .runner import default_state_path
        from .runpod_client import RunpodClient
        from .state_store import StateStore
    except Exception:
        return
    register_routes(server, RouteHandlers(StateStore(default_state_path()), RunpodClient()))


_try_register_routes()
