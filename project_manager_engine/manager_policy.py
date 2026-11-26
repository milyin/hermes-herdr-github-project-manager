"""Hermes tool policy for a manager that can publish a GitHub wiki dashboard."""

from .settings import Hold


TOOLSET = 'project_manager'
DELEGATION_TOOLSET = 'project_manager_delegation'
GITHUB_OPERATIONS = frozenset({'github_handoff', 'github_issues', 'github_pull', 'wiki_read', 'wiki_update'})
SAFE_OPERATIONS = GITHUB_OPERATIONS | {'query', 'mark_read', 'set_comment', 'close', 'workers', 'panes'}
SAFE_TOOLS = frozenset(f'project_manager_{operation}' for operation in SAFE_OPERATIONS)
BRIDGE_TOOLS = frozenset({'tool_search', 'tool_describe', 'tool_call'})
BLOCK = {'action': 'block', 'message': 'The GitHub manager can use only the scoped project-manager tools'}


def veto(tool, args):
    if not isinstance(tool, str):
        return BLOCK
    if tool == 'tool_call':
        calls = args.get('calls') if isinstance(args, dict) else None
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict):
            name = calls[0].get('name')
            if isinstance(name, str) and name in SAFE_TOOLS:
                return None
        return BLOCK
    if tool in SAFE_TOOLS or tool in BRIDGE_TOOLS:
        return None
    return BLOCK


def verify_github_tools(info):
    """Require the gateway's pinned toolset and matching effective inventory."""
    if isinstance(info, dict):
        request = info.get('launch_request')
        inventory = info.get('tools')
        if (isinstance(request, dict) and request.get('toolsets') == [TOOLSET]
                and info.get('enabled_toolsets') == [TOOLSET] and isinstance(inventory, dict)
                and all(isinstance(group, list) and all(isinstance(name, str) for name in group)
                        for group in inventory.values())):
            names = {name for group in inventory.values() for name in group}
            if names <= SAFE_TOOLS | BRIDGE_TOOLS and (
                    SAFE_TOOLS <= names or BRIDGE_TOOLS <= names):
                return
    raise Hold('manager_boundary_unverified', 'GitHub manager requires a verified scoped Hermes session')
