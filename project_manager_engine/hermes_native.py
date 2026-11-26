"""Wire the engine to the selected Hermes profile and the dependency skills."""
import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys

from .engine import private_runtime
from .settings import Hold


ROOT = Path(__file__).resolve().parents[1]
TOOLSET = 'project_manager'
GROUP = 'project-manager'
WATCHDOG_INPUT = 'herdr-project-manager-'


def isolated_python(script, *args, packages=()):
    # The Hermes package manager may activate dependencies outside sys.prefix.
    code = (f'import runpy,sys;sys.path.extend({list(packages)!r});'
            f'sys.path.insert(0,{str(script.parent)!r});'
            f'runpy.run_path({str(script)!r},run_name="__main__")')
    return [sys.executable, '-I', '-B', '-c', code, *map(str, args)]


def package_paths():
    return tuple(str(Path(entry).resolve()) for entry in sys.path
                 if entry and Path(entry).is_dir() and Path(entry).name == 'site-packages')


def hermes_paths():
    import hermes_constants
    return (str(Path(hermes_constants.__file__).resolve().parent), *package_paths())


def hermes_module(module, *args):
    paths = [*hermes_paths(), str(ROOT)]
    code = f'import runpy,sys;sys.path.extend({paths!r});runpy.run_module({module!r},run_name="__main__")'
    return [sys.executable, '-I', '-B', '-c', code, *map(str, args)]


def address(home, workspace, pane):
    key = hashlib.sha256(json.dumps([str(home.resolve()), workspace, pane]).encode()).hexdigest()
    return '\0hermes-pm-' + key


def process_state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def read_viewer(home, workspace, pane):
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(0.6)
            conn.connect(address(home, workspace, pane))
            with conn.makefile('rb') as stream:
                row = json.loads(stream.readline(8193))
        if not isinstance(row, dict) or row.get('pane_id') != pane or row.get('workspace_id') != workspace:
            return {}
        return row
    except (OSError, ValueError, UnicodeError):
        return {}


class Native:
    def __init__(self, home):
        self.home = home
        self.settings = None

    def configure(self, settings):
        self.settings = settings

    def setup_environment(self, agent, cwd, parameters, *, target, workspace):
        manager = agent is self.settings.manager
        request = dict(argv=isolated_python(self.settings.setup_hook), cwd=cwd, stdin=json.dumps(dict(
            agent=agent.name, backend=agent.backend, manager=manager, working_directory=cwd, parameters=parameters)))
        unverified = f'The bound {agent.backend} runtime could not verify environment setup for {agent.name}'
        try:
            if manager:
                sid = self.verify_manager_target(target)
                binding = dict(session_id=sid, cwd=cwd)
                reply = self.gateway('setup_environment', sid, request)
            else:
                pane, thread = target['pane_id'], target['thread_id']
                binding = dict(pane_id=pane, thread_id=thread, cwd=cwd)
                reply = self.agent('setup-environment', workspace, pane, '--thread-id', thread,
                                   '--request-stdin', payload=request)
        except (Hold, KeyError) as exc:
            raise Hold('setup_environment_unverifiable', unverified) from exc
        if (not isinstance(reply, dict) or reply.get('binding') != binding
                or type(reply.get('returncode')) is not int or not isinstance(reply.get('stdout'), str)
                or len(reply['stdout']) > 16384):
            raise Hold('setup_environment_unverifiable', unverified)
        failure = f'Environment setup failed for {agent.name}'
        try:
            values = json.loads(reply['stdout'])
        except ValueError as exc:
            raise Hold('setup_hook_failed', failure) from exc
        if reply['returncode']:
            if isinstance(values, dict) and isinstance(values.get('error'), str) and values['error'].strip():
                failure = f'{failure}. {values["error"]}'
            raise Hold('setup_hook_failed', failure)
        if (not isinstance(values, dict) or values.keys() != agent.prompt.setup_keys
                or any(not isinstance(value, str) or '\0' in value for value in values.values())):
            raise Hold('setup_hook_failed', f'Environment setup for {agent.name} returned invalid prompt values')
        receipt = reply.get('setup_receipt')
        if not isinstance(receipt, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', receipt):
            raise Hold('setup_environment_unverifiable', unverified)
        return values, receipt

    def script(self, skill, filename):
        skill_dir = self.home / 'skills' / skill
        script = (skill_dir / 'scripts' / filename).resolve()
        if not script.is_relative_to(skill_dir.resolve()) or not script.is_file() or not (skill_dir / 'SKILL.md').is_file():
            raise Hold('missing_dependency', f'Install {skill} in this Hermes profile')
        return script

    def invoke(self, argv, payload=None, timeout=80):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False) + '\n'
        try:
            child = subprocess.run(argv, input=data, capture_output=True, encoding='utf-8',
                                   env=dict(os.environ, HERMES_HOME=str(self.home)), timeout=timeout)
            reply = json.loads(child.stdout)
            if not isinstance(reply, dict):
                raise ValueError
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise Hold('transport_error', 'Dependency returned no verified reply') from exc
        if reply.get('ok') is False:
            error = dict(reply.get('error') or {})
            code = error.pop('code', 'dependency_refused')
            message = error.pop('message', 'The dependency refused the operation')
            if code == 'partial_create':
                code = 'partial_provision'
            raise Hold(code, message, **error)
        if child.returncode:
            raise Hold('dependency_refused', 'Dependency process exited without a verified successful reply')
        return reply.get('result', reply)

    def pane(self, operation, *args, workspace=None, payload=None):
        scope = ['--instance', self.settings.instance]
        if workspace is not None:
            scope.extend(['--workspace', workspace])
        script = self.script('hermes-herdr-pane-manager', 'pane_manager.py')
        return self.invoke(isolated_python(script, operation, *scope, *args), payload)

    def agent(self, operation, workspace=None, pane=None, *args, payload=None):
        scope = [] if workspace is None else ['--instance', self.settings.instance, '--workspace', workspace]
        if pane is not None:
            scope.extend(['--pane', pane])
        script = self.script('hermes-herdr-agent-manager', 'agent_manager.py')
        return self.invoke(isolated_python(script, operation, *scope, *args, packages=package_paths()), payload)

    @staticmethod
    def launch_options(profile):
        return ['--launch-profile', profile] if profile else []

    def preflight(self, worker, cwd):
        return self.agent('preflight', None, None, '--agent', worker.backend, '--cwd', cwd,
                          '--model', worker.model, *self.launch_options(worker.launch_profile))

    def provision(self, workspace, worker, cwd):
        return self.agent('provision', workspace, None, '--agent', worker.backend, '--worker',
                          worker.name, '--model', worker.model, '--cwd', cwd, '--group', GROUP,
                          '--status', f'{worker.tag}:RUN', *self.launch_options(worker.launch_profile))

    def quota(self, launch_profile):
        return self.agent('account-quota', None, None, *self.launch_options(launch_profile))

    @property
    def binding(self):
        path = self.home / 'project-manager' / 'manager-session'
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding='utf-8') as stream:
                row = json.load(stream)
                info = os.fstat(stream.fileno())
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError
                if not isinstance(row, dict) or not all(isinstance(row.get(key), str) for key in ('workspace', 'session_id')):
                    raise ValueError
                return row
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise Hold('invalid_manager_binding', 'The Hermes session binding could not be verified') from exc

    def save_binding(self, row):
        path = private_runtime(self.home) / 'manager-session'
        temporary = path.with_name('.manager-session-' + secrets.token_hex(8))
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w',
                           encoding='utf-8') as stream:
                json.dump(row, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def gateway(self, action, *args):
        return asyncio.run(self.gateway_async(action, *args))

    async def gateway_async(self, action, *args):
        from hermes_cli import gateway_client
        from hermes_cli.gateway_runtime import ensure_gateway_runtime, attachable
        submit_attempted = False
        try:
            with gateway_client.outside_restart_wait():
                discovery = await asyncio.to_thread(ensure_gateway_runtime, self.home, idle_exit=False)
                if not attachable(discovery) or discovery.state != 'ready':
                    raise Hold('gateway_unavailable', f'Hermes gateway is {discovery.state}')
                async with gateway_client.connect_gateway(discovery.endpoint) as gateway:
                    if action == 'create':
                        request_id, = args
                        return await gateway.rpc('session.create', request_id=request_id, source='tui',
                                                 cwd=self.settings.manager.cwd, model=self.settings.manager.model,
                                                 _timeout=30)
                    sid, *rest = args
                    if action == 'info':
                        return await gateway.rpc('session.info', session_id=sid, _timeout=10)
                    if action == 'setup_environment':
                        request, = rest
                        return await gateway.rpc('session.environment.setup', session_id=sid, **request, _timeout=40)
                    snapshot = await gateway.rpc('session.resume', session_id=sid, omit_messages=True, _timeout=10)
                    if not isinstance(snapshot, dict) or snapshot.get('session_id') != sid:
                        raise Hold('session_changed', 'Hermes returned a different manager conversation')
                    if action == 'snapshot':
                        return snapshot
                    if action == 'recover_admission':
                        row = unknown_watchdog_admission(snapshot, sid)
                        if row is not None:
                            return await gateway.rpc('prompt.resolve_unknown', session_id=sid,
                                                     admission_id=row['admission_id'],
                                                     execution_generation=row['execution_generation'],
                                                     _timeout=15)
                        return {}
                    if not idle_snapshot(snapshot):
                        raise Hold('manager_busy', 'The Hermes conversation has running or queued input')
                    message, *receipts = rest
                    setup = {'setup_receipt': receipts[0]} if receipts else {}
                    submission_id = WATCHDOG_INPUT + secrets.token_hex(16)
                    submit_attempted = True
                    receipt = await gateway.rpc('prompt.submit', session_id=sid, text=message,
                                                submission_id=submission_id, _timeout=15, **setup)
                    ref = receipt.get('ref') if isinstance(receipt, dict) else None
                    if (not isinstance(ref, dict) or ref.get('session_id') != sid
                            or not isinstance(receipt.get('admission_id'), str)):
                        raise Hold('outcome_unknown', 'Hermes did not confirm the exact admission')
                    return receipt
        except Hold:
            raise
        except gateway_client.GatewayRPCError as exc:
            raise Hold(str(exc), 'Hermes gateway refused the operation') from exc
        except (gateway_client.GatewayClientError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
            known_unsent = (isinstance(exc, gateway_client.GatewayClientError)
                            and str(exc) == getattr(gateway_client, 'NOT_SENT', None))
            code = 'outcome_unknown' if submit_attempted and not known_unsent else 'gateway_unavailable'
            raise Hold(code, 'Hermes gateway did not return a verified reply') from exc

    def prepare_manager(self, workspace, pane):
        manager = self.settings.manager
        if manager.launch_profile is not None:
            raise Hold('launch_profile_unavailable', 'This Hermes gateway cannot apply a named execution profile to a session')
        binding = self.binding
        sid = binding.get('session_id')
        if pane is not None and binding.get('pane_id') == pane['pane_id'] and (
                binding.get('terminal_id') and binding['terminal_id'] != pane.get('terminal_id')):
            raise Hold('manager_terminal_changed', 'Herdr restored the pane with a different terminal')
        if pane is not None and self.manager_exited(workspace, pane):
            self.pane('close', '--pane', pane['pane_id'], '--defer-locked', workspace=workspace)
            pane = None
        if sid is not None:
            try:
                self.gateway('info', sid)
            except Hold as exc:
                if exc.code not in ('not_found', 'session_not_found'):
                    raise
                sid = None
        if sid is None:
            if pane is not None:
                # Preserve an open pane on uncertain state. A connected viewer
                # in a different conversation may be in active operator use.
                raise Hold('manager_session_missing', 'The manager pane is still present but its Hermes conversation is gone')
            seed = [str(self.home.resolve()), workspace, binding.get('session_id'), manager.model, manager.cwd]
            request = 'project-manager-' + hashlib.sha256(json.dumps(seed).encode()).hexdigest()
            created = self.gateway('create', request)
            sid = created.get('session_id')
            if not isinstance(sid, str) or not sid:
                raise Hold('manager_session_unknown', 'Hermes did not identify the manager conversation')
            self.save_binding(dict(workspace=workspace, instance=self.settings.instance, session_id=sid))
        elif binding.get('workspace') != workspace:
            self.save_binding(dict(workspace=workspace, instance=self.settings.instance, session_id=sid))
        if pane is not None:
            if not binding.get('pane_id'):
                self.remember_terminal(workspace, sid, pane)
            return pane
        command = hermes_module('project_manager_engine.viewer', self.home, workspace, sid)
        receipt = self.pane('create', '--cwd', manager.cwd, '--group', GROUP, '--status', 'MGR',
                            '--argv-stdin', workspace=workspace, payload=command)
        self.remember_terminal(workspace, sid, receipt)
        # The caller will wait for the TUI through the ordinary inspection path
        # on subsequent ticks. The exact pane ID is already recoverable.
        return dict(pane_id=receipt['pane_id'], terminal_id=receipt.get('terminal_id'),
                    status='MGR', user_locked=False,
                    displayed_id=receipt.get('displayed_id'))

    def remember_terminal(self, workspace, sid, pane):
        self.save_binding(dict(workspace=workspace, session_id=sid, instance=self.settings.instance,
                               pane_id=pane['pane_id'], terminal_id=pane.get('terminal_id')))

    def manager_exited(self, workspace, pane):
        if pane.get('user_locked') is not False or read_viewer(self.home, workspace, pane['pane_id']):
            return False
        info = self.pane('process-info', '--pane', pane['pane_id'], workspace=workspace)
        processes = info.get('foreground_processes')
        return (info.get('terminal_id') == pane.get('terminal_id') and isinstance(processes, list)
                and len(processes) == 1 and processes[0].get('pid') == info.get('shell_pid')
                and any('pane-manager] command exited' in arg for arg in processes[0].get('argv', [])))

    def manager_status(self, workspace, pane):
        viewer = read_viewer(self.home, workspace, pane['pane_id'])
        bound = self.binding
        sid = bound.get('session_id')
        if not viewer or bound.get('workspace') != workspace or viewer.get('session_id') != sid:
            return dict(attached=False, state='unknown')
        if viewer.get('suspended') is True:
            return dict(attached=True, state='suspended')
        snapshot = self.gateway('snapshot', sid)
        if read_viewer(self.home, workspace, pane['pane_id']) != viewer:
            return dict(attached=False, state='unknown')
        info = snapshot.get('info') or {}
        applied = info.get('launch_request') or {}
        cwd = info.get('cwd') or applied.get('cwd')
        model = info.get('model') or applied.get('model')
        if cwd != self.settings.manager.cwd or model != self.settings.manager.model.rsplit(':', 1)[-1]:
            raise Hold('manager_policy_mismatch', 'Manager conversation model or working directory does not match the profile')
        tools = {name for names in (info.get('tools') or {}).values() for name in names}
        deferred = TOOLSET in (info.get('enabled_toolsets') or ()) and {'tool_search', 'tool_call'} <= tools
        if 'project_manager_run' not in tools and not deferred:
            raise Hold('manager_tools_unavailable', 'The bound manager conversation has no project-manager toolset')
        pending = snapshot.get('pending')
        state = ('blocked' if snapshot.get('prompts') or snapshot.get('open_requests') else
                 'idle' if idle_snapshot(snapshot) else 'working')
        return dict(attached=True, session_id=sid, state=state,
                    pending=[row.get('admission_id') for row in pending] if isinstance(pending, list) else None,
                    watchdog_admission_unknown=unknown_watchdog_admission(snapshot, sid) is not None)

    def verify_manager_target(self, row):
        pane = row['pane_id']
        binding = self.binding
        workspace, sid = binding['workspace'], binding['session_id']
        viewer = read_viewer(self.home, workspace, pane)
        if row.get('session_id') != sid or viewer.get('session_id') != sid:
            raise Hold('manager_detached', 'Manager TUI is not attached to the bound conversation')
        if viewer.get('suspended') is not False:
            raise Hold('manager_suspended', 'The manager TUI is suspended')
        lock = self.pane('lock-status', '--pane', pane, workspace=workspace)
        if lock.get('user_locked') is not False:
            raise Hold('manager_locked', 'The operator has locked the manager pane')
        return sid

    def submit(self, row, message, *, setup_receipt=None):
        receipt = () if setup_receipt is None else (setup_receipt,)
        return self.gateway('submit', self.verify_manager_target(row), message, *receipt)

    def recover_manager(self, row):
        return self.gateway('recover_admission', self.verify_manager_target(row))


def idle_snapshot(snapshot):
    return (snapshot.get('running') is False and snapshot.get('pending') == []
            and not snapshot.get('prompts') and not snapshot.get('open_requests'))


def unknown_watchdog_admission(snapshot, session_id):
    pending = snapshot.get('pending')
    if snapshot.get('running') is not False or not isinstance(pending, list) or not pending:
        return None
    row = pending[0]
    if (isinstance(row, dict) and row.get('status') == 'unknown'
            and (row.get('ref') or {}).get('session_id') == session_id
            and str(row.get('input_id', '')).startswith(WATCHDOG_INPUT)
            and isinstance(row.get('admission_id'), str) and type(row.get('execution_generation')) is int):
        return row
    return None
