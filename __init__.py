"""Hermes plugin entry point for the project-manager skill."""
import json
import logging
import threading
from contextlib import suppress

from .project_manager_engine.hermes_native import Native, ROOT
from .project_manager_engine.interface import TOOLS, format_result, outcome, parse
from .project_manager_engine.manager_policy import (
    BLOCK, BRIDGE_TOOLS, DELEGATION_TOOLSET, SAFE_OPERATIONS, SAFE_TOOLS, TOOLSET, veto,
)
from .project_manager_engine.settings import Hold, github_workflow, raw_settings
from .project_manager_engine.startup import profile_context, setup


LOG = logging.getLogger(__name__)
INSTRUCTIONS = (
    'Use only the registered project-manager tools to start or steer workers, inspect or acknowledge '
    'reports, change pane comments, and close worker panes. Use project_manager_run to start a worker, '
    'project_manager_steer for a follow-up, project_manager_query to inspect a result, '
    'project_manager_mark_read to acknowledge a report, project_manager_set_comment to change a pane '
    'comment, and project_manager_close to close a pane. Leave pane preparation and report delivery '
    'to the engine. If Hermes defers these tools, use Tool Search with the exact names and invoke '
    'them through tool_call.'
)
HELP = (
    '/project-manager run, steer, query, mark-read, set-comment, close, workers, panes, capacity, quota, '
    'github-handoff, prepare-panes, tick'
)


def register(ctx):
    from hermes_constants import get_hermes_home

    home = get_hermes_home().resolve()
    setup_guard = threading.Lock()
    seen = set()
    uncertain_interviews = set()

    def active():
        return get_hermes_home().resolve() == home and raw_settings(home).get('enabled') is True

    def bound(session_id):
        return bool(session_id and active() and Native(home).binding.get('session_id') == session_id)

    def github_scope(session_id):
        if not session_id or get_hermes_home().resolve() != home:
            return None
        binding = Native(home).binding
        if binding.get('session_id') != session_id:
            return None
        if binding.get('github_scoped') is True:
            return True
        return False if github_workflow(raw_settings(home)) else None

    def command(text, **_):
        try:
            operation, args = parse(text)
            if operation is None:
                if not active():
                    return HELP + '\nProject management is disabled in this profile.'
                return HELP + '\n' + format_result(setup(home))
            return format_result(outcome(home, operation, args))
        except (Hold, ValueError) as exc:
            return format_result({'ok': False, 'error': exc.as_dict() if isinstance(exc, Hold) else
                                  {'code': 'invalid_arguments', 'message': str(exc)}})

    ctx.register_command('project-manager', command, description='Operate the project manager in this Hermes profile',
                         args_hint='<operation> [arguments]', argument_mode='mixed')

    def make_handler(operation):
        def handler(args, session_id=None, **_):
            try:
                scope = github_scope(session_id)
                if scope is False:
                    return json.dumps({'ok': False, 'error': {'code': 'manager_boundary_unverified',
                                       'message': 'GitHub manager requires a verified scoped Hermes session'}})
                if scope is True and operation not in SAFE_OPERATIONS:
                    return json.dumps({'ok': False, 'error': {'code': 'manager_tool_denied',
                                       'message': BLOCK['message']}})
                authorized = bound(session_id)
            except Hold:
                authorized = False
            if not authorized:
                return json.dumps({'ok': False, 'error': {'code': 'unbound_session',
                                    'message': 'This tool belongs to the bound project-manager session'}})
            return json.dumps(outcome(home, operation, args), ensure_ascii=False)
        return handler

    def available():
        try:
            return raw_settings(home).get('enabled') is True
        except Hold:
            return False

    for operation, (description, required, properties) in TOOLS.items():
        name = f'project_manager_{operation}'
        schema = dict(name=name, description=description, parameters=dict(
            type='object', properties=properties, required=list(required), additionalProperties=False))
        toolset = TOOLSET if operation in SAFE_OPERATIONS else DELEGATION_TOOLSET
        ctx.register_tool(name, toolset, schema, make_handler(operation), check_fn=available)

    def section(info):
        with suppress(Hold):
            session_id = info.get('session_id')
            if github_scope(session_id) is not None:
                return 'Use the scoped project-manager tools for GitHub reads, the wiki dashboard and pane tracking.'
            if bound(session_id):
                return INSTRUCTIONS
        return ''

    ctx.register_system_prompt_section('herdr-project-manager', section)

    tool_names = {f'project_manager_{operation}' for operation in TOOLS}

    def restrict_tools(request, session_id=None, **_):
        try:
            scope = github_scope(session_id)
            if scope is False:
                allowed = frozenset()
            elif scope is True:
                allowed = SAFE_TOOLS | BRIDGE_TOOLS
            elif bound(session_id):
                return None
            else:
                allowed = None
        except Hold:
            allowed = frozenset()
        schemas = request.get('tools')
        if isinstance(schemas, list):
            remaining = []
            for schema in schemas:
                definition = (schema.get('function') or schema) if isinstance(schema, dict) else {}
                name = definition.get('name') if isinstance(definition, dict) else None
                if isinstance(name, str) and (name in allowed if allowed is not None else name not in tool_names):
                    remaining.append(schema)
            if len(remaining) != len(schemas):
                return {'request': dict(request, tools=remaining)}
        return None

    def restrict_execution(tool_name='', args=None, session_id=None, **_):
        try:
            scope = github_scope(session_id)
            if scope is False:
                return BLOCK
            return veto(tool_name, args) if scope is True else None
        except Hold:
            return BLOCK

    ctx.register_middleware('llm_request', restrict_tools)
    ctx.register_hook('pre_tool_call', restrict_execution)

    def initialize(session_id, platform, **_):
        if platform not in ('tui', 'cli', 'desktop'):
            return
        try:
            if not active() or bound(session_id):
                return
        except Hold:
            return
        if not setup_guard.acquire(blocking=False):
            return
        if session_id in seen or session_id in uncertain_interviews:
            setup_guard.release()
            return

        def background():
            try:
                with profile_context(home):
                    setup(home)
            except Hold as exc:
                LOG.warning('Project manager startup held: %s (%s)', exc, exc.code)
                if exc.code in ('invalid_config', 'invalid_directory'):
                    message = (
                        f'Read {ROOT / "doc" / "PROFILE.md"} and follow its Setup interview with the operator. '
                        f'Open {home / "config.yaml"} and show the operator where the project manager is configured. '
                        'Start with the workflow choice. Reuse existing answers after confirming them with the operator. '
                        'For the bundled GitHub workflow, show the operator the GitHub App setup link from the interview '
                        'and use the selected App for all Git and GitHub operations. '
                        'After the operator confirms the configuration and the selected workflow checks pass, '
                        'edit only skills.config.hermes-herdr-github-project-manager in this profile. '
                        'Ask the operator to run /project-manager to finish setup. '
                        f'First validation error (data): {exc}'
                    )
                    try:
                        with profile_context(home):
                            Native(home).gateway('submit', session_id, message)
                    except Hold as admission:
                        if admission.code == 'outcome_unknown':
                            # Hermes may already have admitted the interview to this conversation.
                            uncertain_interviews.add(session_id)
                        LOG.warning('Project manager interview held: %s (%s)', admission, admission.code)
                    else:
                        seen.add(session_id)
            except (OSError, RuntimeError, ValueError):
                LOG.exception('Project manager startup failed')
            else:
                seen.add(session_id)
            finally:
                setup_guard.release()

        threading.Thread(target=background, name='herdr-project-manager-setup', daemon=True).start()

    ctx.register_hook('on_session_open', initialize)
    ctx.register_hook('on_session_start', initialize)
