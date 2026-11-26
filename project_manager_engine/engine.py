"""Project-agnostic scheduling over the pane, agent, and Hermes adapters."""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .managed_work import BINDING, ManagedWork, open_root, write_record
from .settings import Hold, Settings, directory, identifier, raw_settings


WORKER_OPERATIONS = ('run', 'steer', 'query', 'mark_read', 'set_comment', 'close',
                     'workers', 'panes', 'capacity', 'quota')
COMMANDS = (*WORKER_OPERATIONS, 'prepare_panes', 'tick')
OCCUPIED = {'starting', 'working', 'blocked', 'suspended', 'unknown'}
# Match the agent manager's text input limit before touching the workspace.
MAX_WORKER_PROMPT_BYTES = 32 * 1024
# The receiver reports deadline_elapsed before any prompt bytes are written.
# Partial writes, reply deadlines and failures after admission use other codes.
RUN_REFUSALS = {'user_locked', 'lock_unknown', 'worker_unavailable', 'turn_active', 'already_started',
                'invalid_config', 'invalid_prompt', 'invalid_parameters', 'quota_account_changed', 'quota_unavailable',
                'quota_behind_pace', 'quota_exhausted', 'receiver_unavailable', 'codex_rejected', 'deadline_elapsed'}


def private_runtime(home):
    path = home / 'project-manager'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077 or not path.is_dir():
        raise Hold('invalid_runtime', 'Project-manager runtime directory must be private')
    return path


@contextmanager
def profile_lock(home):
    path = private_runtime(home) / 'lock'
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def validate_worker_prompt(prompt):
    refusal = dict(phase='validation', admission='not_attempted')
    if not isinstance(prompt, str) or not prompt.strip():
        raise Hold('invalid_prompt', 'Worker prompt must be nonempty text', **refusal)
    try:
        size = len(prompt.encode('utf-8'))
    except UnicodeEncodeError as exc:
        raise Hold('invalid_prompt', 'Worker prompt must be valid UTF-8', **refusal) from exc
    if size > MAX_WORKER_PROMPT_BYTES:
        raise Hold('invalid_prompt', f'Worker prompt exceeds {MAX_WORKER_PROMPT_BYTES} UTF-8 bytes',
                   prompt_bytes=size, max_prompt_bytes=MAX_WORKER_PROMPT_BYTES, **refusal)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def require_turn_model_options(report, worker):
    if worker.reasoning_effort is not None and report.get('turn_model_options') is not True:
        raise Hold('turn_model_options_unavailable',
                   'The agent manager and worker receiver must support per-turn reasoning_effort', worker=worker.name)


def issued(reply, pane, thread=None):
    if (not identifier(reply.get('turn_id')) or not identifier(reply.get('thread_id'))
            or reply.get('status') != 'accepted' or (thread is not None and reply['thread_id'] != thread)):
        raise Hold('outcome_unknown', 'Backend did not confirm turn admission', pane_id=pane)
    return dict(reply, pane_id=pane)


def quota_plan(report, now):
    windows = []
    for key in ('primary', 'secondary'):
        window = report.get(key)
        if not isinstance(window, dict):
            continue
        try:
            reset = datetime.fromisoformat(window['reset_at'].replace('Z', '+00:00')).timestamp()
            duration = 60 * window['window_minutes']
            remaining = window['remaining_percent']
            if not 0 <= remaining <= 100 or duration <= 0 or reset <= now:
                continue
        except (KeyError, AttributeError, ValueError, TypeError):
            continue
        used = 100 - remaining
        allowance = max(0, 100 * (1 - (reset - now) / duration))
        windows.append(dict(window=key, used_percent=used, allowance_percent=allowance, reset_at=window['reset_at'],
                            hold=remaining == 0 or used > allowance + 1e-9))
    return windows


class Engine:
    def __init__(self, home, native):
        self.home = home
        self.native = native
        self.workspace_id = None

    def execute(self, command, **kwargs):
        if command not in COMMANDS:
            raise Hold('invalid_command', f'Unknown project-manager command {command}')
        raw = raw_settings(self.home)
        if raw.get('enabled') is not True:
            if command == 'tick':
                return {'action': 'disabled'}
            raise Hold('disabled', 'Enable project management in the selected Hermes profile')
        with profile_lock(self.home):
            raw = raw_settings(self.home)
            if raw.get('enabled') is not True:
                return {'action': 'disabled'}
            self.settings = Settings.read(self.home, raw)
            self.native.configure(self.settings)
            self.workspace_id = None
            self.launches = self.read_launches()
            self.work = ManagedWork(private_runtime(self.home))
            return getattr(self, command)(**kwargs)

    def read_launches(self):
        launches = {}
        for path in private_runtime(self.home).glob('launch-*.json'):
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding='utf-8') as stream:
                    record = json.load(stream)
                if not isinstance(record, dict) or not all(identifier(record.get(key)) for key in (
                        'instance', 'workspace_id', 'pane_id', 'terminal_id', 'worker', 'thread_id')):
                    raise ValueError
                if any(key in record and not identifier(record[key]) for key in
                       ('turn_id', 'parameters_fingerprint', 'work_key', 'working_directory')):
                    raise ValueError
            except (OSError, ValueError) as exc:
                raise Hold('invalid_launch_record', 'A pending worker admission record could not be verified') from exc
            launches[path] = record
        return launches

    def remember_launch(self, path, record):
        write_record(path, record, replace=path in self.launches)
        self.launches[path] = record

    def forget_launch(self, path):
        path.unlink()
        del self.launches[path]

    def pane_launch(self, pane_id):
        return next((path for path, record in self.launches.items() if (
            record['instance'], record['workspace_id'], record['pane_id']) == (
                self.settings.instance, self.workspace_id, pane_id)), None)

    def workspace(self, *, create=False):
        if self.workspace_id is not None:
            return self.workspace_id
        identity = hashlib.sha256(os.fsencode(self.home.resolve())).hexdigest()[:10]
        label = f'{self.settings.manager.name} [{identity}]'
        rows = self.native.pane('workspaces')['workspaces']
        binding = self.native.binding
        if binding and binding.get('instance', self.settings.instance) != self.settings.instance:
            raise Hold('workspace_changed', 'The bound manager belongs to another Herdr instance')
        if bound := binding.get('workspace'):
            if any(row['workspace_id'] == bound for row in rows):
                self.workspace_id = bound
                return bound
        matches = [row['workspace_id'] for row in rows if row.get('label') == label]
        if binding and matches:
            raise Hold('workspace_changed', 'Another Herdr workspace is using the managed workspace label')
        if len(matches) > 1:
            raise Hold('ambiguous_workspace', 'Multiple Herdr workspaces match this profile')
        if not matches and create:
            matches = [self.native.pane('workspace-create', '--label', label, '--group', 'project-manager',
                                        '--cwd', self.settings.manager.cwd)['workspace_id']]
        self.workspace_id = matches[0] if matches else None
        return self.workspace_id

    def layout(self):
        workspace = self.workspace()
        if workspace is None:
            return []
        layout = self.native.pane('layout', workspace=workspace)
        if layout.get('workspace_id') != workspace:
            raise Hold('workspace_changed', 'Herdr returned a different workspace')
        return [dict(pane, tab_zoomed=tab['zoomed']) for tab in layout['tabs'] for pane in tab['panes']]

    def row(self, raw, *, inspect=True):
        pane_id = raw['pane_id']
        status = raw.get('status') or ''
        tag, separator, mark = status.partition(':')
        if status in ('MGR', 'QUOTA'):
            role = 'manager' if status == 'MGR' else 'quota'
        elif separator and mark.split(':')[0] in ('START', 'RUN', 'UNREAD', 'READ', 'WAIT', 'ACK', '?'):
            role = 'worker'
        else:
            return None
        worker = next((value for value in self.settings.workers.values() if value.tag == tag), None)
        lock = {True: 'locked', False: 'unlocked'}.get(raw.get('user_locked'), 'unknown')
        answer = dict(pane_id=pane_id, role=role, status=status, comment=raw.get('comment', ''),
                      presence='yes', lock=lock, state='unknown', delivery='none',
                      pending_closure=raw.get('pending_closure') is True,
                      displayed_id=raw.get('displayed_id'), terminal_id=raw.get('terminal_id'))
        if raw.get('agent_status') in ('idle', 'working', 'blocked', 'done'):
            answer['herdr_status'] = raw['agent_status']
        if raw.get('creation_pending') is True:
            answer.update(state='starting', creation_pending=True)
            if worker:
                answer['worker'] = worker.name
            if role != 'worker':
                return answer
        try:
            if role == 'manager':
                answer.update(self.native.manager_status(self.workspace_id, raw))
            elif role == 'worker':
                if not inspect:
                    return answer
                if worker is None:
                    raise Hold('unknown_worker', f'No configured worker uses pane tag {tag}')
                answer['worker'] = worker.name
                reply = self.agent('status', pane_id)
                if reply.get('worker') != worker.name or reply.get('agent') != worker.backend:
                    raise Hold('binding_changed', 'The pane is bound to a different worker')
                answer.update({key: reply[key] for key in ('state', 'thread_id', 'turn_id', 'completed_at',
                               'working_directory', 'attention', 'active', 'recoverable',
                               'turn_model_options') if key in reply})
                attention = answer.get('attention')
                if attention:
                    mark_code, _, attention_id = mark.partition(':')
                    answer['delivery'] = ('read' if mark_code == 'ACK' else 'unread') if (
                        mark_code in ('WAIT', 'ACK') and attention_id == attention.get('attention_id')) else 'none'
                elif answer['state'] == 'done':
                    answer['delivery'] = {'UNREAD': 'unread', 'READ': 'read'}.get(mark, 'none')
                elif answer['state'] == 'unknown':
                    answer['delivery'] = 'unknown'
        except Hold as exc:
            answer.update(state='unknown', reason=exc.as_dict())
        if role == 'worker':
            self.observe_launch(answer, mark)
        if lock == 'locked' or (role == 'worker' and lock == 'unknown'):
            answer.setdefault('native_state', answer['state'])
            answer['state'] = 'working' if lock == 'locked' else 'unknown'
        return answer

    def observe_launch(self, row, mark):
        path = self.pane_launch(row['pane_id'])
        if path is not None:
            launch = self.launches[path]
            turn = launch.get('turn_id')
            binding_fields = ('terminal_id', 'worker', 'thread_id')
            if 'working_directory' in launch:
                binding_fields += ('working_directory',)
            if any(row.get(key) != launch[key] for key in binding_fields):
                row['recoverable'] = False
                reason = Hold('binding_unverified', 'The recorded submission binding cannot be verified')
                row['reason'] = reason.as_dict()
            else:
                observed = row.get('turn_id')
                if turn is None and identifier(observed) and self.has_managed_admission(launch, row):
                    turn = observed
                    self.record_launch_turn(path, turn)
                if turn is not None and observed == turn and row['state'] in ('working', 'done', 'blocked', 'suspended'):
                    row.update(admission='accepted', admitted_turn_id=turn)
                    return
                if turn is not None:
                    reason = Hold('turn_unverified', 'The admitted turn is not verified in the current worker status')
                elif row['state'] in ('blocked', 'suspended'):
                    reason = Hold('managed_turn_identity_unavailable',
                                  'The agent-manager receiver must identify the managed turn separately from '
                                  'operator attention or expose managed-run metadata for the exact active turn',
                                  dependency='hermes-herdr-agent-manager')
                else:
                    reason = Hold('outcome_unknown', 'The initial prompt admission is unverified')
            row.update(admission='accepted' if turn else 'unknown', state='unknown', delivery='unknown')
            if turn is not None:
                row['admitted_turn_id'] = turn
            row.pop('turn_id', None)
            row.pop('attention', None)
            row.setdefault('reason', reason.as_dict())
        elif mark == 'START':
            row['admission'] = 'not_attempted'
            if row['state'] in ('idle', 'unknown'):
                row['native_state'] = row['state']
                row['state'] = 'starting'
            if not row.get('thread_id'):
                row.setdefault('reason', Hold('attachment_unverified', 'The worker attachment is unverified').as_dict())
        elif mark == 'RUN':
            if identifier(row.get('thread_id')) and identifier(row.get('turn_id')):
                row['admission'] = 'accepted'
            else:
                row.update(admission='unknown', state='unknown', delivery='unknown')
                row.setdefault('reason', Hold('outcome_unknown', 'The pane has no verified initial admission').as_dict())

    def has_managed_admission(self, launch, row):
        if row['state'] in ('working', 'done'):
            # These states carry the receiver's tracked managed turn ID.
            return True
        if row['state'] not in ('blocked', 'suspended') or not launch.get('parameters_fingerprint'):
            return False
        # Attention status can carry an operator turn. Bridge.result omits metadata
        # while active, but the exact turn may have completed since the status read.
        try:
            result = self.agent('result', row['pane_id'], '--turn-id', row['turn_id'])
        except Hold:
            return False
        parameters = result.get('parameters')
        return (result.get('thread_id') == launch['thread_id'] and result.get('turn_id') == row['turn_id']
                and result.get('state') == 'done'
                and isinstance(parameters, dict) and bool(parameters)
                and fingerprint(parameters) == launch['parameters_fingerprint'])

    def panes(self, **filters):
        accepted = {'role', 'worker', 'pane_id', 'state', 'delivery', 'presence', 'lock'}
        if not filters.keys() <= accepted:
            raise Hold('invalid_filter', 'Unknown pane filter')
        rows = [self.row(row) for row in self.layout()]
        rows = [row for row in rows if row and all(row.get(key) == value for key, value in filters.items())]
        return dict(workspace_id=self.workspace_id, panes=sorted(rows, key=lambda row: (
            (row.get('attention') or {}).get('observed_at') or row.get('completed_at') or '', row['pane_id'])))

    def worker(self, name):
        try:
            return self.settings.workers[name]
        except KeyError as exc:
            raise Hold('unknown_worker', f'Unknown configured worker {name}') from exc

    def workers(self):
        return {'workers': [dict(name=worker.name, pane_tag=worker.tag, description=worker.description,
                                 agent=worker.backend, model=worker.model, reasoning_effort=worker.reasoning_effort,
                                 parameters=worker.prompt.defaults,
                                 required=sorted(k for k, v in worker.prompt.defaults.items() if v is None))
                            for worker in self.settings.workers.values()]}

    def agent(self, op, pane_id=None, *args, **kwargs):
        return self.native.agent(op, self.workspace_id, pane_id, *args, **kwargs)

    def quota(self, worker=None):
        workers = [self.worker(worker)] if worker else list(self.settings.workers.values())
        groups = {}
        for setting in workers:
            if setting.backend == 'codex':
                groups.setdefault(setting.launch_profile, []).append(setting.name)
        reports = []
        now = datetime.now(timezone.utc).timestamp()
        for profile, names in groups.items():
            try:
                report = self.native.quota(profile)
                windows = quota_plan(report, now)
                reason = ('quota_unavailable' if not report.get('account') or not windows else
                          'quota_unavailable' if report.get('ordinary_usage_allowed') is False else
                          'quota_behind_pace' if any(window['hold'] for window in windows) else None)
                reports.append(dict(workers=names, account=report.get('account'), windows=windows,
                                    observed_at=report.get('observed_at'), hold=reason))
            except Hold as exc:
                reports.append(dict(workers=names, windows=[], hold=exc.code, reason=str(exc)))
        return dict(accounts=reports, hold=any(report['hold'] for report in reports),
                    applicable=bool(groups))

    def worker_slots(self, rows):
        occupied_states = {row['pane_id']: row['state'] for row in rows if row['state'] in OCCUPIED}
        return dict(maximum=self.settings.limit, available=max(0, self.settings.limit - len(occupied_states)),
                    occupied_states=occupied_states)

    def worker_rows(self, layout):
        return [row for raw in layout if raw.get('status') not in ('MGR', 'QUOTA')
                and (row := self.row(raw)) and row['role'] == 'worker']

    def check_work_cwd(self, cwd):
        if any(Path(cwd).is_relative_to(self.work.path(key)) for key in self.work.records):
            raise Hold('directory_in_use', 'Only the original assignment may use a recorded managed directory')

    def preflight_root(self, setting):
        self.check_work_cwd(setting.work_root)
        try:
            with open_root(setting.work_root):
                self.preflight(setting, setting.work_root)
        except OSError as exc:
            raise Hold('invalid_work_root', 'Managed work root must be an existing directory without symlinks') from exc

    def preflight(self, setting, cwd, *, turn_model_options=False):
        report = self.native.preflight(setting, cwd)
        require_turn_model_options(report, setting)
        if turn_model_options and report.get('turn_model_options') is not True:
            raise Hold('turn_model_options_unavailable', 'The agent-manager launcher lacks per-turn model support')
        if setting.reasoning_effort is not None:
            efforts = report.get('reasoning_efforts')
            if not isinstance(efforts, list) or not all(identifier(effort) for effort in efforts):
                raise Hold('invalid_response', 'The agent manager did not report the model’s reasoning efforts')
            if setting.reasoning_effort not in efforts:
                raise Hold('invalid_config',
                           f'Worker {setting.name}.reasoning_effort {setting.reasoning_effort!r} '
                           f'is unsupported by model {setting.model!r}. Supported values: {", ".join(efforts) or "none"}')

    def capacity(self):
        layout = self.layout()
        rows = self.worker_rows(layout)
        slots = self.worker_slots(rows)
        quota = self.quota()
        held = {name: account['hold'] for account in quota['accounts'] for name in account['workers']}
        ready = {}
        for setting in self.settings.workers.values():
            reason = 'capacity' if not slots['available'] else held.get(setting.name)
            if not reason:
                try:
                    self.check_directory(setting.cwd, rows)
                    if setting.work_root:
                        self.preflight_root(setting)
                    else:
                        self.check_work_cwd(setting.cwd)
                        self.preflight(setting, setting.cwd)
                except Hold as exc:
                    reason = exc.code
            ready[setting.name] = dict(ready=reason is None, hold=reason)
        unresolved = [dict(pane_id=row['pane_id'], worker=row.get('worker'), admission=row['admission'],
                           presence='yes', lock=row['lock'], reason=row.get('reason'))
                      for row in rows if row.get('admission') in ('not_attempted', 'unknown')]
        visible = {raw['pane_id'] for raw in layout}
        for record in self.launches.values():
            if 'turn_id' in record:
                continue
            current_workspace = (record['instance'], record['workspace_id']) == (self.settings.instance, self.workspace_id)
            if not current_workspace or not any(row['pane_id'] == record['pane_id'] for row in rows):
                presence = 'unknown' if not current_workspace else 'yes' if record['pane_id'] in visible else 'no'
                unresolved.append(dict(
                    {key: record[key] for key in ('instance', 'workspace_id', 'pane_id', 'terminal_id', 'worker', 'thread_id')},
                    admission='unknown', presence=presence))
        result = dict(slots, occupied=list(slots['occupied_states']), workers=ready, unresolved_launches=unresolved)
        if self.work.records:
            result['managed_work'] = [
                dict(path=str(self.work.path(key)), worker=record['worker'], workspace_id=record['workspace_id'],
                     pane_id=(record.get('binding') or {}).get('pane_id'), turn_id=record.get('turn_id'))
                for key, record in sorted(self.work.records.items())]
        return result

    def worker_pane(self, pane_id):
        for raw in self.layout():
            if raw['pane_id'] == pane_id:
                row = self.row(raw)
                if row and row['role'] == 'worker' and row.get('worker'):
                    return row
                raise Hold('unknown_worker_pane', 'The exact worker binding could not be verified', pane_id=pane_id)
        path = self.pane_launch(pane_id)
        if path is not None:
            if turn := self.launches[path].get('turn_id'):
                raise Hold('pane_not_found', 'The pane for the admitted turn is absent from the bound workspace',
                           pane_id=pane_id, presence='no', admission='accepted', turn_id=turn)
            raise Hold('outcome_unknown', 'The worker pane is absent and its submission is still unverified',
                       pane_id=pane_id, presence='no', admission='unknown')
        raise Hold('pane_not_found', 'The pane is absent from the bound workspace', pane_id=pane_id)

    def tracked(self, pane_id):
        row = self.worker_pane(pane_id)
        if (not identifier(row.get('thread_id')) or row.get('admission') in ('unknown', 'not_attempted')
                or row.get('reason', {}).get('code') == 'binding_unverified'):
            raise Hold('unknown_worker_pane', 'The exact active worker binding could not be verified', pane_id=pane_id)
        return row

    def rename(self, pane_id, field, value):
        return self.native.pane('rename', '--pane', pane_id, '--' + field, value, workspace=self.workspace_id)

    def check_admission(self, setting, *, rows=None):
        rows = self.panes(role='worker')['panes'] if rows is None else rows
        if not self.worker_slots(rows)['available']:
            raise Hold('capacity', 'No working pane slots are available')
        quota = self.quota(setting.name)
        if quota['hold']:
            report = quota['accounts'][0]
            raise Hold(report['hold'], 'Worker account quota holds new work', worker=setting.name)
        return quota['accounts'][0]['account'] if quota['accounts'] else None

    @staticmethod
    def check_directory(cwd, rows):
        if cwd is not None and any(row.get('working_directory') == cwd for row in rows):
            raise Hold('directory_in_use', 'An open worker pane owns this directory')
        if any((row['state'] in ('unknown', 'starting') or row.get('admission') in ('not_attempted', 'unknown'))
               and not row.get('working_directory') for row in rows):
            raise Hold('outcome_unknown', 'A retained worker directory cannot be verified')

    def run(self, name, prompt='', parameters=None, working_directory=None):
        setting = self.worker(name)
        text, values = setting.prompt.render(parameters)
        if not isinstance(prompt, str):
            raise Hold('invalid_prompt', 'Prompt must be text', phase='validation', admission='not_attempted')
        body = dict(prompt=text + ('\n\n' + prompt if prompt else ''), parameters=values)
        validate_worker_prompt(body['prompt'])
        cwd = setting.cwd if working_directory is None else directory(working_directory)
        dispatch = fingerprint([name, body['prompt']])
        pending = private_runtime(self.home) / f'launch-{dispatch}.json'
        if pending in self.launches:
            record = self.launches[pending]
            if turn := record.get('turn_id'):
                raise Hold('already_started', 'This initial prompt was already admitted', pane_id=record['pane_id'],
                           workspace_id=record['workspace_id'], thread_id=record['thread_id'], turn_id=turn,
                           admission='accepted')
            raise Hold('outcome_unknown', 'This initial prompt already has an unverified submission',
                       pane_id=record['pane_id'], workspace_id=record['workspace_id'], admission='unknown')
        if cwd is None:
            self.preflight_root(setting)
        else:
            self.check_work_cwd(cwd)
            self.preflight(setting, cwd)
        self.workspace(create=True)
        rows = self.panes(role='worker')['panes']
        account = self.check_admission(setting, rows=rows)
        self.check_directory(cwd, rows)
        work_key = None
        if cwd is None:
            work_key = self.work.allocate(setting, self.settings.instance, self.workspace_id)
            cwd = str(self.work.path(work_key))
        pane = None
        try:
            provision = self.native.provision(self.workspace_id, setting, cwd)
            pane = provision.get('pane_id')
            if not identifier(pane):
                raise Hold('partial_provision', 'Provisioning did not identify the worker pane')
            row = self.worker_pane(pane)
            thread = provision.get('thread_id')
            if (row.get('admission') != 'not_attempted' or row['worker'] != name
                    or not identifier(row.get('terminal_id')) or not identifier(thread)
                    or row.get('thread_id') != thread or row.get('working_directory') != cwd):
                raise Hold('attachment_unverified', 'The provisioned worker attachment could not be verified')
            if (row['lock'] != 'unlocked' or row.get('native_state') != 'idle' or row['pending_closure']
                    or 'turn_id' not in row or row['turn_id'] is not None or row.get('active') is not False):
                raise Hold('worker_unavailable', 'The provisioned worker is unavailable for first submission')
            require_turn_model_options(row, setting)
        except Hold as exc:
            details = dict(exc.details, admission='not_attempted', phase='provision', workspace_id=self.workspace_id)
            if identifier(pane):
                details['pane_id'] = pane
            code = 'partial_provision' if exc.code in ('transport_error', 'outcome_unknown', 'invalid_response') else exc.code
            raise Hold(code, str(exc), **details) from exc
        try:
            launch = dict(instance=self.settings.instance, workspace_id=self.workspace_id,
                          pane_id=pane, terminal_id=row['terminal_id'], worker=name, thread_id=thread,
                          parameters_fingerprint=fingerprint(values))
            if work_key:
                self.work.update(work_key, binding={key: launch[key] for key in BINDING})
                launch.update(work_key=work_key, working_directory=cwd)
            self.remember_launch(pending, launch)
        except OSError as exc:
            raise Hold('launch_record_unavailable', 'Could not record the initial prompt before sending it',
                       pane_id=pane, admission='not_attempted', phase='submission') from exc
        try:
            if row.get('turn_model_options') is True:
                body['reasoning_effort'] = setting.reasoning_effort
            reply = self.agent('run', pane, '--agent', setting.backend, '--cwd', cwd,
                               '--model', setting.model, '--request-stdin', payload=dict(body, account=account))
            receipt = issued(reply, pane, thread)
        except Hold as exc:
            if exc.code in RUN_REFUSALS:
                self.forget_launch(pending)
                raise Hold(exc.code, str(exc), pane_id=pane, admission='not_attempted', phase='submission',
                           cleanup=self.defer_unused_close(pane)) from exc
            raise Hold('outcome_unknown', str(exc), pane_id=pane, thread_id=thread,
                       workspace_id=self.workspace_id, admission='unknown', phase='submission', cause=exc.code) from exc
        try:
            self.record_launch_turn(pending, receipt['turn_id'])
            self.rename(pane, 'status', f'{setting.tag}:RUN')
        except (Hold, OSError):
            # The verified receipt is valid even if the label update must wait.
            pass
        if work_key:
            receipt['working_directory'] = cwd
        return dict(receipt, admission='accepted')

    def record_launch_turn(self, path, turn):
        record = self.launches[path]
        if 'turn_id' not in record:
            key = record.get('work_key')
            work = self.work.records.get(key)
            if work and self.work_matches(key, record):
                self.work.update(key, turn_id=turn)
            self.remember_launch(path, dict(record, turn_id=turn))

    def finish_launch(self, path, row):
        if row['status'].endswith(':START'):
            worker = self.worker(row['worker'])
            self.rename(row['pane_id'], 'status', f'{worker.tag}:RUN')
        self.forget_launch(path)

    def steer(self, pane_id, prompt):
        validate_worker_prompt(prompt)
        row = self.tracked(pane_id)
        if (row['lock'] != 'unlocked' or row['state'] not in ('idle', 'done')
                or row['pending_closure']):
            raise Hold('not_idle', 'The bound worker pane is unavailable for a follow-up', pane_id=pane_id)
        worker = self.worker(row['worker'])
        require_turn_model_options(row, worker)
        if row.get('turn_model_options') is True:
            self.preflight(worker, directory(row.get('working_directory')), turn_model_options=True)
        account = self.check_admission(worker)
        body = dict(prompt=prompt, account=account)
        if row.get('turn_model_options') is True:
            body.update(model=worker.model, reasoning_effort=worker.reasoning_effort)
        if launch := self.pane_launch(pane_id):
            self.finish_launch(launch, row)
        self.rename(pane_id, 'status', f'{worker.tag}:RUN')
        key = self.work_for_pane(pane_id, row=row)
        if key:
            # Keep an ambiguous follow-up from looking like the previous turn.
            self.work.update(key, turn_id=None)
        try:
            reply = self.agent('steer', pane_id, '--thread-id', row['thread_id'], '--return-admission',
                               '--request-stdin', payload=body)
            receipt = issued(reply, pane_id, row['thread_id'] if key else None)
        except Hold as exc:
            if exc.code not in ('outcome_unknown', 'transport_error', 'invalid_response',
                                'codex_disconnected', 'hermes_disconnected'):
                current = self.tracked(pane_id)
                if (current.get('turn_id'), current.get('thread_id')) == (row.get('turn_id'), row['thread_id']):
                    self.rename(pane_id, 'status', row['status'])
            raise
        if key:
            try:
                self.work.update(key, turn_id=receipt['turn_id'])
            except OSError:
                # A successful admission remains valid even if the journal update fails.
                pass
        return receipt

    def query(self, pane_id, turn_id=None):
        row = self.worker_pane(pane_id)
        if (row.get('admission') in ('unknown', 'not_attempted') or not identifier(row.get('thread_id'))
                or (row.get('admitted_turn_id') and row.get('reason', {}).get('code') == 'binding_unverified')):
            return row
        if turn_id is None and row.get('admitted_turn_id') and row['state'] != 'done':
            return row
        if turn_id is None:
            turn_id = row.get('turn_id')
        if turn_id is None:
            return row
        result = self.agent('result', pane_id, '--turn-id', turn_id)
        if (result.get('thread_id'), result.get('turn_id')) != (row['thread_id'], turn_id):
            raise Hold('binding_unverified', 'The worker result did not identify the requested thread and turn',
                       pane_id=pane_id, turn_id=turn_id)
        delivery = row['delivery'] if row.get('turn_id') == turn_id else 'none'
        return dict(result, pane_id=pane_id, worker=row['worker'], delivery=delivery)

    def mark_read(self, pane_id, turn_id, attention_id=None):
        row = self.tracked(pane_id)
        if row['lock'] != 'unlocked':
            raise Hold('user_locked', 'The worker pane is locked')
        if row.get('turn_id') != turn_id:
            raise Hold('stale_turn', 'The pane has moved on to another turn')
        worker = self.worker(row['worker'])
        if attention_id is not None:
            if (row.get('attention') or {}).get('attention_id') != attention_id:
                raise Hold('stale_attention', 'The escalation is no longer current')
            status = f'{worker.tag}:ACK:{attention_id}'
        elif row['state'] == 'done':
            status = f'{worker.tag}:READ'
        else:
            raise Hold('not_done', 'The exact worker result is not complete')
        if row['status'] != status:
            self.rename(pane_id, 'status', status)
        return dict(pane_id=pane_id, turn_id=turn_id, attention_id=attention_id, delivery='read')

    def set_comment(self, pane_id, comment):
        self.tracked(pane_id)
        return self.rename(pane_id, 'comment', comment)

    def work_for_pane(self, pane_id, *, row=None):
        matches = [key for key, record in self.work.records.items()
                   if (record['instance'], record['workspace_id']) == (self.settings.instance, self.workspace_id)
                   and (record.get('binding') or {}).get('pane_id') == pane_id
                   and (row is None or self.work_matches(key, row))]
        return matches[0] if len(matches) == 1 else None

    def work_matches(self, key, row):
        record = self.work.records[key]
        return ((record['instance'], record['workspace_id'], record['worker']) ==
                (self.settings.instance, self.workspace_id, row.get('worker'))
                and record.get('binding') == {field: row.get(field) for field in BINDING}
                and row.get('working_directory') == str(self.work.path(key)))

    def close(self, pane_id):
        try:
            row = self.worker_pane(pane_id)
        except Hold as exc:
            if exc.code == 'pane_not_found' and self.workspace_id is not None:
                receipt = dict(pane_id=pane_id, closed=True)
                if key := self.work_for_pane(pane_id):
                    receipt['directory_cleanup'] = self.work.retention(key)
                return receipt
            raise
        if row.get('admission') == 'unknown':
            raise Hold('outcome_unknown', 'Resolve the worker submission before closing the pane',
                       pane_id=pane_id, admission='unknown')
        if row.get('admission') == 'not_attempted':
            self.check_unused_pane(row)
            return self.defer_unused_close(pane_id)
        if (row.get('native_state', row['state']) not in ('idle', 'done')
                or not identifier(row.get('thread_id'))):
            raise Hold('not_idle', 'Wait for the worker turn to finish before closing its pane')
        launch = self.pane_launch(pane_id)
        if launch is not None and row['lock'] == 'unlocked':
            self.finish_launch(launch, row)
        receipt = self.native.pane('close', '--pane', pane_id, '--defer-locked', workspace=self.workspace_id)
        if key := self.work_for_pane(pane_id, row=row):
            receipt['directory_cleanup'] = self.work.retention(key)
        return receipt

    @staticmethod
    def defer_unused_close(pane_id):
        # The pane guard checks identity and zoom. Only the receiver can hold
        # its shared operator/automation admission lock through a close.
        return dict(pane_id=pane_id, closed=False, status_code='close_deferred', reason='turn_guard_unavailable',
                    dependency='hermes-herdr-agent-manager',
                    message='Closing an unused worker pane requires a receiver operation that verifies '
                            'turn state and holds its admission lock through the guarded pane close')

    @staticmethod
    def check_unused_pane(row):
        pane = row['pane_id']
        if row['lock'] != 'unlocked':
            raise Hold('user_locked' if row['lock'] == 'locked' else 'lock_unknown',
                       'Wait for the worker pane to be verified unlocked', pane_id=pane)
        # A matching receiver with no thread cannot have admitted a Codex turn.
        # An attachment timeout by itself says nothing about the current binding.
        idle = row.get('native_state') == 'idle' and identifier(row.get('thread_id'))
        unattached = row.get('native_state') == 'unknown' and 'thread_id' in row and row['thread_id'] is None
        if (not identifier(row.get('terminal_id')) or 'turn_id' not in row or row['turn_id'] is not None
                or row.get('active') is not False or row.get('herdr_status') == 'working' or not (idle or unattached)):
            raise Hold('worker_unavailable', 'Could not verify that the unsubmitted worker pane has no active turn',
                       pane_id=pane, admission='not_attempted')

    def prepare_manager(self):
        self.workspace(create=True)
        panes = self.layout()
        matches = [pane for pane in panes if pane.get('status') == 'MGR' and not pane.get('pending_closure')]
        if len(matches) > 1:
            raise Hold('manager_ambiguous', 'More than one live pane has the manager role')
        if matches and matches[0].get('creation_pending') and not matches[0].get('user_locked'):
            raw = matches[0]
            if self.native.manager_status(self.workspace_id, raw).get('attached'):
                self.rename(raw['pane_id'], 'status', 'MGR')
                matches = [dict(raw, creation_pending=False)]
        raw = self.native.prepare_manager(self.workspace_id, matches[0] if matches else None)
        if not raw:
            return None
        row = self.row(raw)
        if row['lock'] == 'unlocked' and row.get('watchdog_admission_unknown'):
            try:
                self.native.recover_manager(row)
            except Hold as exc:
                row['reason'] = exc.as_dict()
            else:
                row = self.row(raw)
        return row

    def prepare_workers(self):
        holds = []
        for row in self.panes(role='worker')['panes']:
            pane = row['pane_id']
            if row['lock'] != 'unlocked' or not row.get('worker'):
                continue
            try:
                launch = self.pane_launch(pane)
                if row.get('recoverable') and identifier(row.get('thread_id')) and not row['pending_closure']:
                    self.agent('recover', pane)
                    continue
                if row.get('admission') == 'not_attempted':
                    self.check_unused_pane(row)
                    holds.append(dict(pane_id=pane, error=self.defer_unused_close(pane)))
                    continue
                if row.get('admission') == 'unknown':
                    holds.append(dict(pane_id=pane, error=row['reason']))
                    continue
                if row.get('creation_pending') and row.get('admission') != 'accepted':
                    binding = self.agent('status', pane)
                    if binding.get('thread_id') and binding.get('worker') == row['worker']:
                        self.rename(pane, 'status', row['status'])
                    continue
                if row['pending_closure']:
                    if row['state'] in ('idle', 'done'):
                        self.close(pane)
                    continue
                worker = self.worker(row['worker'])
                attention = row.get('attention')
                if attention and row['delivery'] != 'read':
                    status = f"{worker.tag}:WAIT:{attention['attention_id']}"
                elif row['state'] == 'done' and row['delivery'] == 'none':
                    result = self.query(pane, row['turn_id'])
                    if result['state'] != 'done':
                        continue
                    status = f'{worker.tag}:UNREAD'
                elif row['state'] == 'working' and row['status'].split(':')[1] in ('START', 'WAIT', 'ACK'):
                    status = f'{worker.tag}:RUN'
                else:
                    status = row['status']
                if row['status'] != status:
                    self.rename(pane, 'status', status)
                if launch is not None and row['state'] == 'done' and status in (f'{worker.tag}:UNREAD', f'{worker.tag}:READ'):
                    self.forget_launch(launch)
            except Hold as exc:
                holds.append(dict(pane_id=pane, error=exc.as_dict()))
        return holds

    def prepare_panes(self):
        manager = self.prepare_manager()
        holds = self.prepare_workers()
        return dict(workspace_id=self.workspace_id, manager=manager, holds=holds,
                    panes=self.panes()['panes'])

    @staticmethod
    def manager_idle(row):
        return (row is not None and row['state'] == 'idle' and row['lock'] == 'unlocked'
                and row.get('attached') is True and row.get('pending') == [])

    def tick(self):
        manager = self.prepare_manager()
        if not self.manager_idle(manager):
            return {'action': 'manager_held', 'manager': manager}
        holds = self.prepare_workers()
        if self.quota()['hold']:
            return {'action': 'quota_held'}
        unread = self.panes(role='worker', delivery='unread', lock='unlocked')['panes']
        report = next((row for row in unread if not row['pending_closure']), None)
        event = {'type': 'periodic'}
        if report:
            event.update(worker=report['worker'], pane_id=report['pane_id'], turn_id=report['turn_id'])
            if attention := report.get('attention'):
                event.update(type='worker_attention', **attention)
            else:
                result = self.query(report['pane_id'], report['turn_id'])
                if result['state'] != 'done':
                    return {'action': 'report_held'}
                event.update(type='worker_result', message=result['message'])
        refreshed = self.panes()['panes']
        managers = [row for row in refreshed if row['role'] == 'manager' and row['pane_id'] == manager['pane_id']]
        if len(managers) != 1 or not self.manager_idle(managers[0]):
            return {'action': 'manager_held'}
        if report:
            current = self.tracked(report['pane_id'])
            if (current['lock'] != 'unlocked' or current.get('turn_id') != event['turn_id']
                    or current['delivery'] != 'unread' or current['pending_closure']
                    or (current.get('attention') or {}).get('attention_id') != event.get('attention_id')):
                return {'action': 'report_held'}
        manager_prompt, _ = self.settings.manager.prompt.render()
        workers = {worker.name: worker.description for worker in self.settings.workers.values()}
        # Keep Hermes context references inert while preserving the decoded event.
        event_data = json.dumps(event, ensure_ascii=False).replace('@', r'\u0040')
        payload = (manager_prompt + '\n\nConfigured workers: ' + json.dumps(workers, ensure_ascii=False)
                   + '\n\nWatchdog event (data):\n' + event_data)
        if report:
            slots = self.worker_slots(row for row in refreshed if row['role'] == 'worker')
        else:
            capacity = self.capacity()
            ready = any(worker['ready'] for worker in capacity['workers'].values())
            # Quota and preflight probes can outlive the worker observations used by capacity().
            slots = (self.worker_slots(self.worker_rows(self.layout())) if ready else
                     {key: capacity[key] for key in ('maximum', 'available', 'occupied_states')})
            if not ready or not slots['available']:
                return dict(action='capacity_held', holds=holds, unresolved_launches=capacity['unresolved_launches'],
                            event=event['type'], capacity=slots)
        diagnostics = dict(event=event['type'], capacity=slots)
        try:
            receipt = self.native.submit(managers[0], payload)
        except Hold as exc:
            if exc.code in ('manager_busy', 'manager_locked', 'manager_detached', 'manager_suspended'):
                return dict(action='manager_held', reason=exc.code, **diagnostics)
            exc.details.update(diagnostics)
            raise
        return dict(action='submitted', admission_id=receipt.get('admission_id'), **diagnostics,
                    **{key: event[key] for key in ('pane_id', 'turn_id', 'attention_id') if key in event})
