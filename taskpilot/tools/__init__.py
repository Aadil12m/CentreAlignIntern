from .base import Tool, ToolRegistry, ToolResult
from .browser import BROWSER_TOOLS
from .workspace import WORKSPACE_TOOLS


def default_registry() -> ToolRegistry:
    return ToolRegistry([*BROWSER_TOOLS, *WORKSPACE_TOOLS])


# What the verifier may use. It runs with read_only=True, so form submissions other than
# signing in are refused by RunContext.check_action.
VERIFIER_TOOLS = ["browser_open", "browser_snapshot", "browser_click", "browser_fill", "browser_back", "browser_screenshot",
                  "list_files", "read_file", "http_get"]

__all__ = ["Tool", "ToolRegistry", "ToolResult", "default_registry", "VERIFIER_TOOLS"]
