from .nodes import (
    AgentNode,
    BrowserNode,
    BuildContainerNode,
    ComposeYAMLNode,
    DeployNode,
    GitRepositoryNode,
    KeepAliveNode,
    LanguageRuntimeNode,
    LLMApiNode,
    LLMServerNode,
    LocalSQLDatabaseNode,
    LogsNode,
    MCPServerNode,
    NetworkStorageNode,
    PackageNode,
    RemoteSQLDatabaseNode,
    RunLocalContainersNode,
    RunOnRunpodNode,
    S3StorageNode,
    SkillFrameworkNode,
    SkillNode,
    SSHAccessNode,
    SSHCommandNode,
    StartupScriptNode,
    SubagentNode,
    VectorDatabaseNode,
    WebTerminalNode,
)

NODE_CLASS_MAPPINGS = {
    "Agent": AgentNode,
    "Browser": BrowserNode,
    "WebTerminal": WebTerminalNode,
    "LLMServer": LLMServerNode,
    "LLMApi": LLMApiNode,
    "MCPServer": MCPServerNode,
    "Skill": SkillNode,
    "SkillFramework": SkillFrameworkNode,
    "Subagent": SubagentNode,
    "RemoteSQLDatabase": RemoteSQLDatabaseNode,
    "LocalSQLDatabase": LocalSQLDatabaseNode,
    "VectorDatabase": VectorDatabaseNode,
    "NetworkStorage": NetworkStorageNode,
    "S3Storage": S3StorageNode,
    "SSHCommand": SSHCommandNode,
    "Package": PackageNode,
    "GitRepository": GitRepositoryNode,
    "LanguageRuntime": LanguageRuntimeNode,
    "BuildContainer": BuildContainerNode,
    "KeepAlive": KeepAliveNode,
    "SSHAccess": SSHAccessNode,
    "Deploy": DeployNode,
    "RunOnRunpod": RunOnRunpodNode,
    "StartupScript": StartupScriptNode,
    "ComposeYAML": ComposeYAMLNode,
    "RunLocalContainers": RunLocalContainersNode,
    "Logs": LogsNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Agent": "Agent",
    "Browser": "Browser",
    "WebTerminal": "Web Terminal",
    "LLMServer": "LLM Server",
    "LLMApi": "LLM API",
    "MCPServer": "MCP Server",
    "Skill": "Skill",
    "SkillFramework": "Skill Framework",
    "Subagent": "Subagent",
    "RemoteSQLDatabase": "Remote SQL Database",
    "LocalSQLDatabase": "Local SQL Database",
    "VectorDatabase": "Vector Database",
    "NetworkStorage": "Network Storage",
    "S3Storage": "S3 Storage",
    "SSHCommand": "SSH Command",
    "Package": "Package",
    "GitRepository": "Git Repository",
    "LanguageRuntime": "Language Runtime",
    "BuildContainer": "Build Container",
    "KeepAlive": "Keep Alive",
    "SSHAccess": "SSH Access",
    "Deploy": "Deploy",
    "RunOnRunpod": "Run on Runpod",
    "StartupScript": "Startup Script",
    "ComposeYAML": "Compose YAML",
    "RunLocalContainers": "Run Local Containers",
    "Logs": "Logs",
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
