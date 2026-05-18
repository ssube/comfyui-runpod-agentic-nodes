from .nodes import (
    RunpodAgentNode,
    RunpodBrowserNode,
    RunpodKeepAliveNode,
    RunpodLLMApiNode,
    RunpodLLMServerNode,
    RunpodLogsNode,
    RunpodMCPServerNode,
    RunpodNetworkStorageNode,
    RunpodPodNode,
    RunpodRunNode,
    RunpodS3StorageNode,
    RunpodSkillFrameworkNode,
    RunpodSkillNode,
    RunpodSQLDatabaseNode,
    RunpodSSHAccessNode,
    RunpodSSHCommandNode,
    RunpodVectorDatabaseNode,
)

NODE_CLASS_MAPPINGS = {
    "RunpodAgent": RunpodAgentNode,
    "RunpodBrowser": RunpodBrowserNode,
    "RunpodLLMServer": RunpodLLMServerNode,
    "RunpodLLMApi": RunpodLLMApiNode,
    "RunpodMCPServer": RunpodMCPServerNode,
    "RunpodSkill": RunpodSkillNode,
    "RunpodSkillFramework": RunpodSkillFrameworkNode,
    "RunpodSQLDatabase": RunpodSQLDatabaseNode,
    "RunpodVectorDatabase": RunpodVectorDatabaseNode,
    "RunpodNetworkStorage": RunpodNetworkStorageNode,
    "RunpodS3Storage": RunpodS3StorageNode,
    "RunpodSSHCommand": RunpodSSHCommandNode,
    "RunpodKeepAlive": RunpodKeepAliveNode,
    "RunpodSSHAccess": RunpodSSHAccessNode,
    "RunpodPod": RunpodPodNode,
    "RunpodRun": RunpodRunNode,
    "RunpodLogs": RunpodLogsNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RunpodAgent": "Agent",
    "RunpodBrowser": "Browser",
    "RunpodLLMServer": "LLM Server",
    "RunpodLLMApi": "LLM API",
    "RunpodMCPServer": "MCP Server",
    "RunpodSkill": "Skill",
    "RunpodSkillFramework": "Skill Framework",
    "RunpodSQLDatabase": "SQL Database",
    "RunpodVectorDatabase": "Vector Database",
    "RunpodNetworkStorage": "Network Storage",
    "RunpodS3Storage": "S3 Storage",
    "RunpodSSHCommand": "SSH Command",
    "RunpodKeepAlive": "Keep Alive",
    "RunpodSSHAccess": "SSH Access",
    "RunpodPod": "Runpod Pod",
    "RunpodRun": "Run on Runpod",
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
