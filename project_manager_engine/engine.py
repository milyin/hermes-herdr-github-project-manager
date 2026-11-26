"""Project-agnostic scheduling over the pane, agent, and Hermes adapters."""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from .settings import Hold, Settings, directory, raw_settings


WORKER_OPERATIONS = ('run', 'steer', 'query', 'mark_read', 'set_comment', 'close',
                     'workers', 'panes', 'capacity', 'quota')
COMMANDS = (*WORKER_OPERATIONS, 'prepare_panes', 'tick')
OCCUPIED = {'starting', 'working', 'blocked', 'suspended', 'unknown'}
EXCERPT_LIMIT = 12000


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


def issued(reply, pane):
    if (not reply.get('turn_id') or not reply.get('thread_id')
            or reply.get('status') != 'accepted'):
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
            return getattr(self, command)(**kwargs)

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
        elif separator and mark.split(':')[0] in ('RUN', 'UNREAD', 'READ', 'WAIT', 'ACK', '?'):
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
                               'working_directory', 'attention', 'active', 'recoverable') if key in reply})
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
        if lock == 'locked' or (role == 'worker' and lock == 'unknown'):
            answer['native_state'] = answer['state']
            answer['state'] = 'working' if lock == 'locked' else 'unknown'
        return answer

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
                                 agent=worker.backend, model=worker.model, parameters=worker.prompt.defaults,
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

    def occupied(self, rows):
        return [row['pane_id'] for row in rows if row['state'] in OCCUPIED]

    def capacity(self):
        rows = self.panes(role='worker')['panes']
        occupied = self.occupied(rows)
        available = max(0, self.settings.limit - len(occupied))
        quota = self.quota()
        held = {name: account['hold'] for account in quota['accounts'] for name in account['workers']}
        ready = {}
        for setting in self.settings.workers.values():
            reason = 'capacity' if not available else held.get(setting.name)
            if not reason:
                try:
                    self.check_directory(setting.cwd, rows)
                    self.native.preflight(setting, setting.cwd)
                except Hold as exc:
                    reason = exc.code
            ready[setting.name] = dict(ready=reason is None, hold=reason)
        return dict(maximum=self.settings.limit, occupied=occupied, available=available, workers=ready)

    def tracked(self, pane_id):
        for raw in self.layout():
            if raw['pane_id'] == pane_id:
                row = self.row(raw)
                if row and row['role'] == 'worker' and row.get('thread_id'):
                    return row
                raise Hold('unknown_worker_pane', 'The exact worker binding could not be verified', pane_id=pane_id)
        raise Hold('pane_not_found', 'The pane is absent from the bound workspace', pane_id=pane_id)

    def rename(self, pane_id, field, value):
        return self.native.pane('rename', '--pane', pane_id, '--' + field, value, workspace=self.workspace_id)

    def check_admission(self, setting, *, rows=None):
        rows = self.panes(role='worker')['panes'] if rows is None else rows
        if len(self.occupied(rows)) >= self.settings.limit:
            raise Hold('capacity', 'No working pane slots are available')
        quota = self.quota(setting.name)
        if quota['hold']:
            report = quota['accounts'][0]
            raise Hold(report['hold'], 'Worker account quota holds new work', worker=setting.name)
        return quota['accounts'][0]['account'] if quota['accounts'] else None

    @staticmethod
    def check_directory(cwd, rows):
        if any(row.get('working_directory') == cwd for row in rows):
            raise Hold('directory_in_use', 'An open worker pane owns this directory')
        if any(row['state'] == 'unknown' and not row.get('working_directory') for row in rows):
            raise Hold('outcome_unknown', 'A retained worker directory cannot be verified')

    def run(self, name, prompt='', parameters=None, working_directory=None):
        setting = self.worker(name)
        text, values = setting.prompt.render(parameters)
        if not isinstance(prompt, str):
            raise Hold('invalid_prompt', 'Prompt must be text')
        cwd = setting.cwd if working_directory is None else directory(working_directory)
        self.native.preflight(setting, cwd)
        self.workspace(create=True)
        rows = self.panes(role='worker')['panes']
        account = self.check_admission(setting, rows=rows)
        self.check_directory(cwd, rows)
        try:
            pane = self.native.provision(self.workspace_id, setting, cwd)['pane_id']
        except Hold as exc:
            if exc.code in ('transport_error', 'partial_provision', 'outcome_unknown'):
                raise Hold('outcome_unknown', str(exc), **exc.details) from exc
            raise
        try:
            self.rename(pane, 'status', f'{setting.tag}:RUN')
            body = dict(prompt=text + ('\n\n' + prompt if prompt else ''), parameters=values, account=account)
            return issued(self.agent('run', pane, '--agent', setting.backend, '--cwd', cwd,
                                     '--model', setting.model, '--request-stdin', payload=body), pane)
        except Hold as exc:
            if exc.code in ('outcome_unknown', 'transport_error', 'partial_provision', 'invalid_response',
                            'codex_disconnected', 'hermes_disconnected'):
                raise Hold('outcome_unknown', str(exc), pane_id=pane) from exc
            try:
                state = self.tracked(pane)
                cleanup = self.close(pane) if state['state'] == 'idle' and not state.get('turn_id') else {}
            except Hold:
                cleanup = {'closed': False}
            raise Hold(exc.code, str(exc), **({} if cleanup.get('closed') else {'pane_id': pane})) from exc

    def steer(self, pane_id, prompt):
        row = self.tracked(pane_id)
        if (row['lock'] != 'unlocked' or row['state'] not in ('idle', 'done')
                or row['pending_closure']):
            raise Hold('not_idle', 'The bound worker pane is unavailable for a follow-up', pane_id=pane_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise Hold('invalid_prompt', 'Follow-up must contain text')
        worker = self.worker(row['worker'])
        account = self.check_admission(worker)
        self.rename(pane_id, 'status', f'{worker.tag}:RUN')
        try:
            return issued(self.agent('steer', pane_id, '--thread-id', row['thread_id'], '--return-admission',
                                     '--request-stdin', payload={'prompt': prompt, 'account': account}), pane_id)
        except Hold as exc:
            if exc.code not in ('outcome_unknown', 'transport_error', 'invalid_response',
                                'codex_disconnected', 'hermes_disconnected'):
                current = self.tracked(pane_id)
                if (current.get('turn_id'), current.get('thread_id')) == (row.get('turn_id'), row['thread_id']):
                    self.rename(pane_id, 'status', row['status'])
            raise

    def query(self, pane_id, turn_id=None):
        row = self.tracked(pane_id)
        if turn_id is None:
            turn_id = row.get('turn_id')
        if turn_id is None:
            return row
        result = self.agent('result', pane_id, '--turn-id', turn_id)
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

    def close(self, pane_id):
        try:
            row = self.tracked(pane_id)
        except Hold as exc:
            if exc.code == 'pane_not_found' and self.workspace_id is not None:
                return dict(pane_id=pane_id, closed=True)
            raise
        state = row.get('native_state', row['state'])
        if state not in ('idle', 'done'):
            raise Hold('not_idle', 'Wait for the worker turn to finish before closing its pane')
        return self.native.pane('close', '--pane', pane_id, '--defer-locked', workspace=self.workspace_id)

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
        if row['lock'] == 'unlocked' and row.get('watchdog_interrupted'):
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
                if row.get('creation_pending'):
                    binding = self.agent('status', pane)
                    if binding.get('thread_id') and binding.get('worker') == row['worker']:
                        self.rename(pane, 'status', row['status'])
                    continue
                if row['pending_closure']:
                    if row['state'] in ('idle', 'done'):
                        self.close(pane)
                    continue
                if row.get('recoverable'):
                    self.agent('recover', pane)
                    continue
                if row['state'] == 'idle' and row['status'].endswith(':RUN') and not row.get('turn_id'):
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
                elif row['state'] == 'working' and row['status'].split(':')[1] in ('WAIT', 'ACK'):
                    status = f'{worker.tag}:RUN'
                else:
                    continue
                if row['status'] != status:
                    self.rename(pane, 'status', status)
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
        self.prepare_workers()
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
                message = result['message']
                event.update(type='worker_result', message=message[:EXCERPT_LIMIT])
                if len(message) > EXCERPT_LIMIT:
                    event['truncated'] = True
        elif not any(worker['ready'] for worker in self.capacity()['workers'].values()):
            return {'action': 'capacity_held'}
        refreshed = self.panes(role='manager', pane_id=manager['pane_id'])['panes']
        if len(refreshed) != 1 or not self.manager_idle(refreshed[0]):
            return {'action': 'manager_held'}
        if report:
            current = self.tracked(report['pane_id'])
            if (current['lock'] != 'unlocked' or current.get('turn_id') != event['turn_id']
                    or current['delivery'] != 'unread' or current['pending_closure']
                    or (current.get('attention') or {}).get('attention_id') != event.get('attention_id')):
                return {'action': 'report_held'}
        manager_prompt, _ = self.settings.manager.prompt.render()
        workers = {worker.name: worker.description for worker in self.settings.workers.values()}
        payload = (manager_prompt + '\n\nConfigured workers: ' + json.dumps(workers, ensure_ascii=False)
                   + '\n\nWatchdog event (data):\n' + json.dumps(event, ensure_ascii=False))
        try:
            receipt = self.native.submit(refreshed[0], payload)
        except Hold as exc:
            if exc.code in ('manager_busy', 'manager_locked', 'manager_detached', 'manager_suspended'):
                return {'action': 'manager_held', 'reason': exc.code}
            raise
        return dict(action='submitted', event=event['type'], admission_id=receipt.get('admission_id'),
                    **{key: event[key] for key in ('pane_id', 'turn_id', 'attention_id') if key in event})
