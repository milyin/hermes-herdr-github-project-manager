"""Idempotent Hermes scheduler registration for one selected profile."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import sys

from .engine import profile_lock
from .hermes_native import ROOT, hermes_paths
from .interface import outcome
from .settings import Hold, Settings, raw_settings


WATCHDOG = 'Herdr project-manager watchdog'
SCRIPT = 'herdr-project-manager-watchdog.py'
SCHEDULE = '1m'


@contextmanager
def profile_context(home):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def write_script(home):
    folder = home / 'scripts'
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.resolve().parent != home.resolve():
        raise Hold('invalid_scripts_dir', 'Hermes scripts must be installed inside this profile')
    path = folder / SCRIPT
    if path.is_symlink():
        raise Hold('invalid_watchdog', 'The watchdog entry point must be a regular profile script')
    paths = [*hermes_paths(), str(ROOT)]
    wrapper = (
        'import json, os, sys\n'
        'from pathlib import Path\n'
        f'sys.path.extend({paths!r})\n'
        f'home = Path({str(home)!r})\n'
        'os.environ["HERMES_HOME"] = str(home)\n'
        'from project_manager_engine.interface import outcome\n'
        'result = outcome(home, "tick", {})\n'
        'print(json.dumps(result, ensure_ascii=False))\n'
        'if not result["ok"]:\n'
        '    print(result["error"]["message"], file=sys.stderr)\n'
        '    raise SystemExit(1)\n'
    )
    if path.is_file() and path.read_text(encoding='utf-8') == wrapper:
        return
    temporary = path.with_name('.' + SCRIPT + '-' + secrets.token_hex(8))
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w',
                       encoding='utf-8') as stream:
            stream.write(wrapper)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def verification(job):
    from cron.executions import list_executions
    executions = list_executions(job_id=job['id'], limit=25)
    scheduled = [row for row in executions if row.get('scheduled_instant')]
    if any(row['status'] == 'completed' for row in scheduled):
        return dict(id=job['id'], verification='confirmed', last_scheduled=scheduled[0]['status'])
    if scheduled and scheduled[0]['status'] in ('failed', 'unknown', 'cancelled', 'missed'):
        row = scheduled[0]
        return dict(id=job['id'], verification='failed', reason=row.get('error') or row['status'])
    now = datetime.now(timezone.utc)
    next_run = job.get('next_run_at')
    if isinstance(next_run, str):
        due = datetime.fromisoformat(next_run.replace('Z', '+00:00'))
        if due.tzinfo is not None and (now - due).total_seconds() > 90:
            return dict(id=job['id'], verification='overdue', last_dispatch=job.get('last_dispatch'))
    return dict(id=job['id'], verification='pending', next_run_at=next_run)


def setup(home):
    from cron.jobs import create_job, list_jobs, parse_schedule, update_job
    from hermes_cli.gateway_runtime import attachable, ensure_gateway_runtime
    with profile_context(home), profile_lock(home):
        raw = raw_settings(home)
        if raw.get('enabled') is not True:
            return {'action': 'disabled'}
        settings = Settings.read(home, raw)
        if settings.manager.launch_profile:
            raise Hold('launch_profile_unavailable', 'This Hermes gateway cannot apply a named manager execution profile')
        write_script(home)
        jobs = [job for job in list_jobs(include_disabled=True) if job.get('name') == WATCHDOG]
        if len(jobs) > 1:
            raise Hold('watchdog_ambiguous', 'The profile has multiple project-manager watchdog jobs')
        if not jobs:
            job = create_job(prompt=None, schedule=SCHEDULE, name=WATCHDOG, script=SCRIPT,
                             no_agent=True, deliver='local', interpreter=sys.executable, catch_up=False)
        else:
            job = jobs[0]
            if (job.get('script') != SCRIPT or not job.get('no_agent') or not job.get('enabled')
                    or job.get('schedule') != parse_schedule(SCHEDULE) or job.get('repeat', {}).get('times') is not None):
                raise Hold('watchdog_mismatch', 'The existing watchdog is disabled or has a different script or mode')
            if job.get('interpreter') != sys.executable:
                job = update_job(job['id'], {'interpreter': sys.executable}) or job
        scheduler = verification(job)
    with profile_context(home):
        owner = ensure_gateway_runtime(home, idle_exit=False)
        if not attachable(owner):
            raise Hold('gateway_unavailable', f'Hermes gateway is {owner.state}')
        return dict(watchdog=scheduler, preparation=outcome(home, 'prepare_panes', {}))
