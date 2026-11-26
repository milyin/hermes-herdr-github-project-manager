"""Hermes plugin entry point for the project-manager skill."""
import json
import logging
import threading
from contextlib import suppress

from .project_manager_engine.hermes_native import Native, TOOLSET
from .project_manager_engine.interface import TOOLS, format_result, outcome, parse
from .project_manager_engine.settings import Hold, raw_settings
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
    'prepare-panes, tick'
)


def register(ctx):
    from hermes_constants import get_hermes_home

    home = get_hermes_home().resolve()
    setup_guard = threading.Lock()
    seen = set()

    def active():
        return get_hermes_home().resolve() == home and raw_settings(home).get('enabled') is True

    def bound(session_id):
        return bool(session_id and active() and Native(home).binding.get('session_id') == session_id)

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
        ctx.register_tool(name, TOOLSET, schema, make_handler(operation), check_fn=available)

    def section(info):
        with suppress(Hold):
            if bound(info.get('session_id')):
                return INSTRUCTIONS
        return ''

    ctx.register_system_prompt_section('herdr-project-manager', section)

    tool_names = {f'project_manager_{operation}' for operation in TOOLS}

    def restrict_tools(request, session_id=None, **_):
        with suppress(Hold):
            if bound(session_id):
                return None
        schemas = request.get('tools')
        if isinstance(schemas, list):
            remaining = [schema for schema in schemas if isinstance(schema, dict)
                         and (schema.get('function') or schema).get('name') not in tool_names]
            if len(remaining) != len(schemas):
                return {'request': dict(request, tools=remaining)}
        return None

    ctx.register_middleware('llm_request', restrict_tools)

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
        if session_id in seen:
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
                        'Start the project-manager setup interview with the operator. Ask for values missing or invalid '
                        f'in skills.config.hermes-herdr-github-project-manager in {home / "config.yaml"}. '
                        'Use the installed skill profile documentation. Edit only that settings section after the user '
                        'provides its values, then ask the operator to run /project-manager. '
                        f'First validation error: {exc}'
                    )
                    with profile_context(home), suppress(Hold):
                        Native(home).gateway('submit', session_id, message)
            except (OSError, RuntimeError, ValueError):
                LOG.exception('Project manager startup failed')
            finally:
                seen.add(session_id)
                setup_guard.release()

        threading.Thread(target=background, name='herdr-project-manager-setup', daemon=True).start()

    ctx.register_hook('on_session_open', initialize)
    ctx.register_hook('on_session_start', initialize)
