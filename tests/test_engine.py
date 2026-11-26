import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, PIPE, Popen, TimeoutExpired, run
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from project_manager_engine.engine import Engine, quota_plan
from project_manager_engine.hermes_native import Native, WATCHDOG_INPUT, unknown_watchdog_admission
from project_manager_engine.interface import parse
from project_manager_engine.settings import Hold, SKILL, Settings, raw_settings


GITHUB_WORKFLOW = Path(__file__).resolve().parents[1] / 'workflows' / 'github'


class Dependencies:
    binding = dict(workspace='w1', session_id='manager')

    def __init__(self, cwd):
        self.manager = dict(pane_id='w1:p1', status='MGR', user_locked=False)
        self.worker = dict(pane_id='w1:p2', terminal_id='t2', status='I:RUN', user_locked=False)
        self.rows = [self.manager]
        self.manager_state = dict(attached=True, state='idle', pending=[])
        self.result = dict(worker='worker', agent='codex', thread_id='thread', turn_id='turn', state='done',
                           working_directory=cwd, completed_at='2026-11-26T07:00:00+00:00')
        self.agent_reads = 0
        self.runs = 0
        self.recoveries = 0
        self.closes = []
        self.provision_error = self.run_error = self.status_error = None
        self.rename_errors = {}
        self.turn_results = {}
        self.admitted_thread = 'thread'
        self.submissions = []
        self.quota_hold = False

    def configure(self, settings):
        self.settings = settings

    def pane(self, operation, *args, workspace=None):
        if operation == 'workspaces':
            return {'workspaces': [{'workspace_id': 'w1', 'label': 'test'}]}
        if operation == 'layout':
            return dict(workspace_id=workspace, tabs=[dict(zoomed=False, panes=self.rows)])
        if operation == 'rename':
            if args[3] in self.rename_errors:
                raise self.rename_errors[args[3]]
            row = next(pane for pane in self.rows if pane['pane_id'] == args[1])
            row[args[2][2:]] = args[3]
            return dict(row)
        if operation == 'close':
            assert args == ('--pane', self.worker['pane_id'], '--defer-locked')
            self.closes.append(args[1])
            self.rows.remove(self.worker)
            return dict(pane_id=args[1], closed=True)
        raise AssertionError(operation)

    def manager_status(self, *_):
        return dict(self.manager_state)

    def prepare_manager(self, *_):
        return self.manager

    def agent(self, operation, _workspace, pane, *args, payload=None):
        assert pane == self.worker['pane_id']
        self.agent_reads += 1
        if operation == 'status':
            if self.status_error:
                raise self.status_error
            return dict(self.result)
        if operation == 'result':
            flag, turn = args
            assert flag == '--turn-id'
            if turn in self.turn_results:
                return self.turn_results[turn]
            if self.result['state'] == 'done' and turn == 'turn':
                return dict(self.result, message='finished', parameters={'TASK': 'test'})
            return dict(thread_id=self.result['thread_id'], turn_id=turn, state='working')
        if operation == 'run':
            assert payload['account'] == 'account'
            self.runs += 1
            if self.run_error:
                raise self.run_error
            return dict(status='accepted', thread_id=self.admitted_thread, turn_id='turn')
        if operation == 'steer':
            assert payload['account'] == 'account'
            return dict(status='accepted', thread_id=self.result['thread_id'], turn_id='follow-up')
        if operation == 'recover':
            self.recoveries += 1
            self.result.update(state='idle', recoverable=False)
            return dict(thread_id=self.result['thread_id'], status='resumed')
        raise AssertionError(operation)

    def quota(self, _):
        reset = datetime.now(timezone.utc) + timedelta(hours=1)
        return dict(account='account', ordinary_usage_allowed=True, primary=dict(
            remaining_percent=0 if self.quota_hold else 100, window_minutes=120, reset_at=reset.isoformat()))

    def preflight(self, *_):
        return {'ready': True}

    def provision(self, _workspace, _worker, cwd):
        self.worker['status'] = 'I:START'
        self.rows.append(self.worker)
        self.result.update(state='idle', thread_id='thread', turn_id=None, active=False, working_directory=cwd)
        if self.provision_error:
            self.result.update(state='unknown', thread_id=None)
            raise self.provision_error
        return dict(self.worker, thread_id='thread')

    def submit(self, _row, payload):
        self.submissions.append(json.loads(payload.split('Watchdog event (data):\n', 1)[1]))
        return {'admission_id': f'accepted-{len(self.submissions)}'}


class Contract(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'manager.md').write_text('workflow {{KEY}}')
        (self.home / 'worker.md').write_text('start {{TASK}} with {{PR}}')
        cwd = self.home / 'worker'
        cwd.mkdir()
        self.config = dict(enabled=True, max_working_panes=1, manager=dict(
            name='test-manager', working_directory=str(self.home), model='test',
            description='manager', prompt_file='manager.md', parameters={'KEY': 'value'}),
            workers=[dict(name='worker', working_directory=str(cwd), pane_tag='I', model='test',
                          agent='codex', description='worker', prompt_file='worker.md',
                          parameters={'TASK': None, 'PR': 'none'})])
        self.save()
        self.native = Dependencies(str(cwd))
        self.engine = Engine(self.home, self.native)

    def save(self):
        text = yaml.safe_dump({'skills': {'config': {SKILL: self.config}}})
        (self.home / 'config.yaml').write_text(text)

    def managed_root(self):
        root = self.home / 'managed'
        root.mkdir(mode=0o700)
        self.config['workers'][0].pop('working_directory')
        self.config['workers'][0]['managed_work_root'] = str(root)
        self.native.manager['terminal_id'] = 't1'
        self.save()
        return root

    def managed_run(self, **options):
        return self.engine.execute('run', name='worker', parameters={'TASK': 'fixture'}, **options)

    def finish_managed(self):
        self.native.result.update(state='done', turn_id='turn', active=False)
        self.engine.execute('mark_read', pane_id='w1:p2', turn_id='turn')

    def close_managed(self):
        return self.engine.execute('close', pane_id='w1:p2')['directory_cleanup']

    def test_managed_allocations_are_unique_and_caller_paths_stay_unrecorded(self):
        root = self.managed_root()
        caller = root / 'caller'
        caller.mkdir()
        unrecorded = root / ('worker-' + 'a' * 32)
        unrecorded.mkdir()
        first = Path(self.managed_run()['working_directory'])
        self.assertEqual(first.parent, root)
        self.assertTrue(first.name.startswith('worker-'))
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        self.finish_managed()
        self.engine = Engine(self.home, self.native)
        self.assertEqual(self.close_managed(),
                         dict(path=str(first), status='retained', reason='cleanup_guard_unavailable'))

        second = Path(self.managed_run()['working_directory'])
        self.assertNotEqual(first, second)
        self.finish_managed()
        self.assertEqual(self.close_managed()['path'], str(second))
        for supplied in (caller, self.home / 'worker'):
            self.managed_run(working_directory=str(supplied))
            self.finish_managed()
            self.assertNotIn('directory_cleanup', self.engine.execute('close', pane_id='w1:p2'))
            self.assertTrue(supplied.is_dir())
        self.assertEqual(set(root.iterdir()), {caller, unrecorded, first, second})
        records = self.engine.execute('capacity')['managed_work']
        self.assertEqual({Path(record['path']) for record in records}, {first, second})

    def test_managed_cleanup_preserves_checkout_and_untracked_files(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        # A linked checkout has a metadata pointer outside the working directory.
        git_metadata = self.home / 'external-git'
        git_metadata.mkdir()
        (git_metadata / 'HEAD').write_text('unpublished head')
        (cwd / '.git').write_text(f'gitdir: {git_metadata}\n')
        (cwd / 'local-change').write_text('dirty work')
        self.finish_managed()
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue((cwd / '.git').is_file())
        self.assertEqual((cwd / 'local-change').read_text(), 'dirty work')
        self.assertEqual((git_metadata / 'HEAD').read_text(), 'unpublished head')

    def test_managed_roots_reject_lexical_escape_and_symlink_components(self):
        root = self.managed_root()
        link = self.home / 'root-link'
        link.symlink_to(root, target_is_directory=True)
        (root / 'nested').mkdir(mode=0o700)
        for candidate, code in ((f'{root}/../worker', 'invalid_config'),
                                (str(link), 'invalid_work_root'),
                                (str(link / 'nested'), 'invalid_work_root')):
            with self.subTest(candidate=candidate):
                self.config['workers'][0]['managed_work_root'] = candidate
                self.save()
                with self.assertRaises(Hold) as error:
                    self.managed_run()
                self.assertEqual(error.exception.code, code)
        self.assertEqual(self.native.runs, 0)
        self.assertEqual(list((root / 'nested').iterdir()), [])

    def test_managed_cleanup_retains_replaced_directory_or_root(self):
        root = self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        outside = self.home / 'worker'
        original = root / 'original'
        cwd.rename(original)
        cwd.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue(cwd.is_symlink())
        self.assertTrue(outside.is_dir())
        self.assertTrue(original.is_dir())
        cwd.unlink()
        cwd.mkdir(mode=0o700)
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue(cwd.is_dir())

        retired = self.home / 'retired'
        root.rename(retired)
        root.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue(cwd.is_dir())
        self.assertTrue((retired / cwd.name).is_dir())

    def test_interrupted_managed_provision_and_admission_retain_their_paths(self):
        root = self.managed_root()
        for field in ('provision_error', 'run_error'):
            with self.subTest(failure=field):
                setattr(self.native, field, Hold('transport_error', 'fixture lost its reply'))
                with self.assertRaises(Hold):
                    self.managed_run()
                setattr(self.native, field, None)
                self.native.rows.remove(self.native.worker)
                self.engine = Engine(self.home, self.native)
                self.engine.execute('prepare_panes')
                records = self.engine.execute('capacity')['managed_work']
                self.assertEqual(set(root.iterdir()), {Path(record['path']) for record in records})
                self.assertTrue(all(record['turn_id'] is None for record in records))
        self.assertEqual(len(list(root.iterdir())), 2)
        with self.assertRaises(Hold) as held:
            self.managed_run()
        self.assertEqual(held.exception.code, 'outcome_unknown')

    def test_allocation_record_failure_holds_before_provision(self):
        root = self.managed_root()
        with patch('project_manager_engine.managed_work.write_record', side_effect=OSError('fixture write failed')):
            with self.assertRaises(Hold) as error:
                self.managed_run()
        self.assertEqual(error.exception.code, 'work_allocation_unverified')
        self.assertEqual(self.native.rows, [self.native.manager])
        (cwd,) = root.iterdir()
        self.engine.execute('prepare_panes')
        self.assertTrue(cwd.is_dir())

    def test_recovered_managed_admission_can_be_reconciled_before_close(self):
        root = self.managed_root()
        self.native.run_error = Hold('transport_error', 'fixture lost its admission reply')
        with self.assertRaises(Hold):
            self.managed_run()
        self.engine = Engine(self.home, self.native)
        self.finish_managed()
        (record,) = self.engine.execute('capacity')['managed_work']
        self.assertEqual(record['turn_id'], 'turn')
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertEqual(list(root.iterdir()), [Path(record['path'])])

    def test_managed_close_never_attempts_path_based_removal(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        with patch('os.rmdir', side_effect=AssertionError('unguarded directory removal')) as remove:
            self.assertEqual(self.close_managed()['reason'], 'cleanup_guard_unavailable')
            self.assertEqual(self.close_managed()['reason'], 'cleanup_guard_unavailable')
            self.engine.execute('prepare_panes')
            self.engine.execute('tick')
        remove.assert_not_called()
        self.assertEqual(list(cwd.iterdir()), [])

    def test_missing_pane_without_guarded_close_keeps_managed_directory(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        self.native.rows.remove(self.native.worker)
        self.assertEqual(self.close_managed()['reason'], 'cleanup_guard_unavailable')
        self.assertTrue(cwd.is_dir())

    def test_managed_cleanup_does_not_trust_close_receipts_after_binding_changes(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        pane = self.native.pane

        def defer_close(operation, *args, **kwargs):
            if operation == 'close':
                return dict(pane_id='w1:p2', closed=False, status_code='close_deferred')
            return pane(operation, *args, **kwargs)

        with patch.object(self.native, 'pane', side_effect=defer_close):
            self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue(cwd.is_dir())

        def admit_operator_turn_and_replace_terminal(operation, *args, **kwargs):
            if operation == 'close':
                self.native.result.update(state='working', turn_id='operator-turn', active=True)
                self.native.worker['terminal_id'] = 'replacement'
            result = pane(operation, *args, **kwargs)
            if operation == 'close':
                result.update(terminal_id='t2', thread_id='thread', turn_id='turn')
            return result

        with patch.object(self.native, 'pane', side_effect=admit_operator_turn_and_replace_terminal):
            receipt = self.engine.execute('close', pane_id='w1:p2')
        self.assertTrue(receipt['closed'])
        self.assertEqual(receipt['directory_cleanup']['reason'], 'cleanup_guard_unavailable')
        self.assertTrue(cwd.is_dir())
        (journal,) = (self.home / 'project-manager').glob('work-*.json')
        self.assertNotIn('closed', json.loads(journal.read_text()))

    def test_legacy_close_flag_cannot_authorize_cleanup_on_restart(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        self.native.rows.remove(self.native.worker)
        (journal,) = (self.home / 'project-manager').glob('work-*.json')
        journal.write_text(json.dumps(dict(json.loads(journal.read_text()), closed=True)))
        self.engine = Engine(self.home, self.native)
        with patch('os.rmdir', side_effect=AssertionError('unguarded directory removal')):
            self.assertEqual(self.close_managed()['reason'], 'cleanup_guard_unavailable')
            self.engine.execute('prepare_panes')
            self.engine.execute('tick')
        self.assertTrue(cwd.is_dir())
        self.assertTrue(journal.is_file())

    def test_active_or_unknown_worker_cannot_authorize_cleanup(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        for state in ('working', 'unknown'):
            with self.subTest(state=state):
                self.native.result.update(state=state, active=True)
                with self.assertRaises(Hold):
                    self.close_managed()
                self.assertTrue(cwd.is_dir())
        self.assertEqual(self.native.closes, [])

    def test_unknown_followup_clears_the_recorded_turn_and_retains_the_directory(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()
        agent = self.native.agent

        def lose_followup(operation, *args, **kwargs):
            if operation == 'steer':
                raise Hold('transport_error', 'fixture lost its follow-up reply')
            return agent(operation, *args, **kwargs)

        with patch.object(self.native, 'agent', side_effect=lose_followup):
            with self.assertRaises(Hold):
                self.engine.execute('steer', pane_id='w1:p2', prompt='fixture follow-up')
        (record,) = self.engine.execute('capacity')['managed_work']
        self.assertIsNone(record['turn_id'])
        self.finish_managed()
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue(cwd.is_dir())

    @unittest.skipUnless(sys.platform == 'linux', 'requires Linux per-thread filesystem state')
    def test_managed_close_retains_directory_used_only_by_a_nonleader_thread(self):
        self.managed_root()
        cwd = Path(self.managed_run()['working_directory'])
        self.finish_managed()

        code = """
import ctypes
import os
import sys
import threading

def hold_directory():
    clone_fs = 0x00000200
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(clone_fs) != 0:
        print(f'unshare unavailable: {os.strerror(ctypes.get_errno())}', flush=True)
        return
    os.chdir(sys.argv[1])
    print(threading.get_native_id(), flush=True)
    sys.stdin.read()

worker = threading.Thread(target=hold_directory)
worker.start()
worker.join()
"""
        with Popen([sys.executable, '-B', '-c', code, str(cwd)], cwd=self.home,
                   stdin=PIPE, stdout=PIPE, text=True) as child:
            try:
                tid = child.stdout.readline().strip()
                if tid.startswith('unshare unavailable:'):
                    self.skipTest(tid)
                self.assertTrue(tid.isdecimal(), tid)
                proc = Path(f'/proc/{child.pid}')
                self.assertEqual((proc / 'cwd').readlink(), self.home)
                self.assertEqual((proc / 'task' / tid / 'cwd').readlink(), cwd)
                self.assertEqual(self.close_managed()['reason'], 'cleanup_guard_unavailable')
                self.assertTrue(cwd.is_dir())
            finally:
                child.communicate()
        self.assertEqual(self.close_managed()['status'], 'retained')
        self.assertTrue(cwd.is_dir())

    def test_render_once_and_reject_invalid_parameters(self):
        worker = Settings.read(self.home, raw_settings(self.home)).workers['worker']
        text, parameters = worker.prompt.render({'TASK': '{{PR}}'})
        self.assertEqual(text, 'start {{PR}} with none')
        self.assertEqual(parameters['PR'], 'none')
        with self.assertRaisesRegex(Hold, 'requires TASK'):
            worker.prompt.render()
        with self.assertRaisesRegex(Hold, 'Invalid or unused parameter'):
            worker.prompt.render({'TASK': '', 'UNKNOWN': '?'})
        (self.home / 'worker.md').write_text('{{undeclared}}')
        with self.assertRaisesRegex(Hold, 'worker.*undeclared'):
            self.engine.execute('prepare_panes')

    def test_disabled_and_pending_manager_do_not_read_workers(self):
        self.config['enabled'] = False
        self.save()
        self.assertEqual(self.engine.execute('tick'), {'action': 'disabled'})
        self.assertFalse(hasattr(self.native, 'settings'))
        self.config['enabled'] = True
        self.save()
        self.native.rows.append(self.native.worker)
        self.native.manager_state['pending'] = ['operator']
        self.assertEqual(self.engine.execute('tick')['action'], 'manager_held')
        self.assertEqual(self.native.agent_reads, 0)

    def test_result_retries_until_exact_acknowledgement(self):
        self.native.rows.append(self.native.worker)
        self.assertEqual(self.engine.execute('tick')['event'], 'worker_result')
        self.assertEqual(self.engine.execute('tick')['event'], 'worker_result')
        self.assertEqual(self.native.submissions[0], self.native.submissions[1])
        self.assertEqual(self.native.worker['status'], 'I:UNREAD')
        with self.assertRaisesRegex(Hold, 'another turn'):
            self.engine.execute('mark_read', pane_id='w1:p2', turn_id='old-turn')
        self.engine.execute('mark_read', pane_id='w1:p2', turn_id='turn')
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['delivery'], 'read')
        self.assertEqual(self.engine.execute('capacity')['occupied'], [])

    def test_watchdog_submits_full_literal_worker_result(self):
        message = (' \tCafé @file:notes.md\r\n  $PWD "quoted" \\u0040 {{HEAD}}\n'
                   + 'x' * 12001 + '\n  suffix \n')
        self.native.rows.append(self.native.worker)
        self.native.turn_results['turn'] = dict(self.native.result, message=message)
        with patch.object(self.native, 'submit', wraps=self.native.submit) as submit:
            self.assertEqual(self.engine.execute('tick')['event'], 'worker_result')

        wire = submit.call_args.args[1].split('Watchdog event (data):\n', 1)[1]
        self.assertNotIn('@', wire)
        event = json.loads(wire)
        self.assertNotIn('truncated', event)
        self.assertEqual(event['message'], message)
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2', turn_id='turn')['message'], message)

    def test_github_handoffs_reach_a_recipient_with_only_terminal_input(self):
        adapter = Native(self.home)
        empty_directory = self.home / 'worker'
        terminal = (
            'import json, sys\n'
            'prompt = json.load(sys.stdin)["prompt"]\n'
            'print(json.dumps(dict(ok=True, result=dict(status="accepted", thread_id="thread", '
            'turn_id=sys.argv[1], transcript=prompt))))\n'
        )
        agent = self.native.agent

        def isolated_child(*args, env, **kwargs):
            return run(*args, env={}, cwd=empty_directory, **kwargs)

        def deliver(operation, workspace, pane, *options, payload=None):
            if operation in ('run', 'steer'):
                self.assertEqual(options[-1], '--request-stdin')
                turn = 'turn' if operation == 'run' else 'follow-up'
                with patch('project_manager_engine.hermes_native.subprocess.run', side_effect=isolated_child):
                    return adapter.invoke([sys.executable, '-I', '-S', '-c', terminal, turn],
                                          {'prompt': payload['prompt']})
            return agent(operation, workspace, pane, *options, payload=payload)

        repo, issue, pr = 'owner/repository', '12', '34'
        reviewed_head, new_head = 'a' * 40, 'b' * 40
        implementation = ('  parser.py:38 now preserves CRLF.\r\n'
                          '  python -m unittest test_parser → 8 passed.\n'
                          '  Regression text: {{HEAD}} $PWD @file:fixture\n\n')
        findings = ('  parser.py:38 strips whitespace from the final line.\r\n'
                    '  Observed: "end" for input "end  ". Expected: "end  ".\n'
                    '  Preserve the trailing spaces and add a regression case.\n  ')
        evidence = f'Issue #{issue}, PR #{pr}, head {new_head}\n\n{implementation}'
        follow_up = f'{repo} issue #{issue}, PR #{pr}, reviewed head {reviewed_head}\n\n{findings}'
        worker = self.config['workers'][0]
        worker.update(prompt_file=str(GITHUB_WORKFLOW / 'REVIEWER_PROMPT.md'),
                      parameters=dict(REPO=None, ISSUE=None, PR=None, HEAD=None, github_identity='fixture-app'))
        self.save()
        with patch.object(self.native, 'agent', side_effect=deliver):
            reviewer = self.engine.execute('run', name='worker', prompt=evidence,
                                           parameters=dict(REPO=repo, ISSUE=issue, PR=pr, HEAD=new_head))
            self.assertTrue(reviewer['transcript'].endswith('\n\n' + evidence))

            self.native.result.update(state='done', turn_id='turn')
            self.engine.execute('prepare_panes')
            self.native.rows.remove(self.native.worker)
            worker.update(prompt_file=str(GITHUB_WORKFLOW / 'IMPLEMENTER_PROMPT.md'),
                          parameters=dict(REPO=None, ISSUE=None, PR=None, TASK=None, github_identity='fixture-app'))
            self.save()
            implementer = self.engine.execute('run', name='worker',
                                              parameters=dict(REPO=repo, ISSUE=issue, PR=pr, TASK=follow_up))
            self.assertIn('\n\n' + follow_up + '\n\n', implementer['transcript'])

            self.native.result.update(state='done', turn_id='turn')
            steered = self.engine.execute('steer', pane_id=implementer['pane_id'], prompt=follow_up)
            self.assertEqual(steered['transcript'], follow_up)

    def test_run_checks_rendered_utf8_limit_before_provisioning(self):
        parameters = {'TASK': 'Evidence 🧪', 'PR': '34'}
        rendered = 'start Evidence 🧪 with 34\n\n'
        evidence = '  café @file:fixture\r\n'
        limit = 32768
        prompt = evidence + ' ' * (limit - len((rendered + evidence).encode('utf-8')))
        with (patch.object(self.native, 'preflight', wraps=self.native.preflight) as preflight,
              patch.object(self.native, 'provision', wraps=self.native.provision) as provision,
              patch.object(self.native, 'pane', wraps=self.native.pane) as pane):
            with self.assertRaises(Hold) as rejected:
                self.engine.execute('run', name='worker', parameters=parameters, prompt=prompt + 'x')
        self.assertEqual(rejected.exception.code, 'invalid_prompt')
        self.assertEqual(rejected.exception.details, dict(
            phase='validation', admission='not_attempted', prompt_bytes=limit + 1, max_prompt_bytes=limit))
        preflight.assert_not_called()
        provision.assert_not_called()
        pane.assert_not_called()
        self.assertEqual(self.native.runs, 0)
        self.assertEqual(self.native.closes, [])
        capacity = self.engine.execute('capacity')
        self.assertEqual(capacity['available'], 1)
        self.assertEqual(capacity['unresolved_launches'], [])
        self.assertEqual(self.engine.execute('tick')['event'], 'periodic')

        with patch.object(self.native, 'agent', wraps=self.native.agent) as agent:
            receipt = self.engine.execute('run', name='worker', parameters=parameters, prompt=prompt)
        self.assertEqual(receipt['admission'], 'accepted')
        self.assertEqual(agent.call_args.args[0], 'run')
        self.assertEqual(agent.call_args.kwargs['payload']['prompt'], rendered + prompt)

    def test_oversized_handoff_can_use_an_existing_idle_pane_when_it_fits_steer(self):
        pane = self.native.worker['pane_id']
        self.native.worker['status'] = 'I:UNREAD'
        self.native.rows.append(self.native.worker)
        prior = self.engine.execute('query', pane_id=pane, turn_id='turn')
        references = f'owner/repository issue #12, PR #34 at {"a" * 40}\n\n'
        evidence = 'Fix the CRLF handler. Evidence starts here.\r\n  🧪 café\n'
        prompt = references + evidence
        prompt += ' ' * (32768 - len(prompt.encode('utf-8')))

        with (patch.object(self.native, 'pane', wraps=self.native.pane) as operations,
              patch.object(self.native, 'agent', wraps=self.native.agent) as agent):
            with self.assertRaises(Hold) as oversized_run:
                self.engine.execute('run', name='worker', parameters={'TASK': 'Review feedback'}, prompt=prompt)
            with self.assertRaises(Hold) as oversized_steer:
                self.engine.execute('steer', pane_id=pane, prompt=prompt + 'x')
            with self.assertRaises(Hold) as invalid_utf8:
                self.engine.execute('steer', pane_id=pane, prompt='\ud800')
        self.assertEqual(oversized_run.exception.details['phase'], 'validation')
        self.assertEqual(oversized_steer.exception.code, 'invalid_prompt')
        self.assertEqual(oversized_steer.exception.details, dict(
            phase='validation', admission='not_attempted', prompt_bytes=32769, max_prompt_bytes=32768))
        self.assertEqual(invalid_utf8.exception.code, 'invalid_prompt')
        self.assertEqual(invalid_utf8.exception.details['admission'], 'not_attempted')
        operations.assert_not_called()
        agent.assert_not_called()
        self.assertEqual(self.engine.execute('query', pane_id=pane, turn_id='turn'), prior)
        self.assertEqual(self.native.worker['status'], 'I:UNREAD')
        self.assertEqual(self.engine.execute('capacity')['available'], 1)

        with patch.object(self.native, 'agent', wraps=self.native.agent) as agent:
            receipt = self.engine.execute('steer', pane_id=pane, prompt=prompt)
        self.assertEqual(receipt['turn_id'], 'follow-up')
        self.assertEqual(agent.call_args.args[0], 'steer')
        self.assertEqual(agent.call_args.kwargs['payload']['prompt'], prompt)
        self.assertEqual(self.native.runs, 0)

    def test_oversized_batch_can_be_reassigned_as_independent_terminal_turns(self):
        head = 'a' * 40
        new_head = 'b' * 40
        references = f'owner/repository issue #12, PR #34, reviewed head {head}\n'
        first = 'parser.py:38: preserve trailing spaces.\r\n  Fixture output:\n' + 'é' * 9000 + '  \n'
        second = 'writer.py:21: retain the final newline.\r\n  Fixture output:\n' + '🧪' * 4500 + '\n'
        first_task = references + f'Working head {head}\n\n' + first
        second_task = references + f'Working head {new_head}\n\n' + second

        with self.assertRaises(Hold) as combined:
            self.engine.execute('run', name='worker', parameters={'TASK': first_task + second})
        self.assertEqual(combined.exception.details['admission'], 'not_attempted')
        self.assertEqual(self.native.runs, 0)

        with patch.object(self.native, 'agent', wraps=self.native.agent) as agent:
            admitted = self.engine.execute('run', name='worker', parameters={'TASK': first_task})
            self.assertEqual(admitted['admission'], 'accepted')
            self.assertEqual(agent.call_args.kwargs['payload']['prompt'], 'start ' + first_task + ' with none')
            self.native.result.update(state='done', turn_id=admitted['turn_id'])
            self.native.turn_results[admitted['turn_id']] = dict(
                self.native.result, message=f'First finding fixed, pushed {new_head}.')
            result = self.engine.execute('query', pane_id=admitted['pane_id'], turn_id=admitted['turn_id'])
            self.assertEqual(result['message'], f'First finding fixed, pushed {new_head}.')
            self.engine.execute('mark_read', pane_id=admitted['pane_id'], turn_id=admitted['turn_id'])

            follow_up = self.engine.execute('steer', pane_id=admitted['pane_id'], prompt=second_task)
            self.assertEqual(follow_up['turn_id'], 'follow-up')
            self.assertEqual(agent.call_args.kwargs['payload']['prompt'], second_task)
        self.assertEqual(self.native.runs, 1)

    def test_periodic_is_held_with_a_verified_busy_worker_at_limit_one(self):
        self.native.rows.append(self.native.worker)
        self.native.result.update(state='working', active=True)
        row, = self.engine.execute('panes', role='worker')['panes']
        self.assertEqual((row['state'], row['admission'], row['delivery']), ('working', 'accepted', 'none'))

        tick = self.engine.execute('tick')

        self.assertEqual((tick['action'], tick['event']), ('capacity_held', 'periodic'))
        self.assertEqual(tick['capacity'], dict(maximum=1, available=0, occupied_states={'w1:p2': 'working'}))
        self.assertEqual(self.native.submissions, [])

    def test_periodic_rechecks_slots_when_a_worker_becomes_busy_during_preflight(self):
        self.native.worker['status'] = 'I:READ'
        self.native.rows.append(self.native.worker)
        self.native.result['working_directory'] = str(self.home)
        self.assertTrue(self.engine.execute('capacity')['workers']['worker']['ready'])

        def start_turn(*_):
            self.native.result.update(state='working', turn_id='next-turn', active=True)
            return {'ready': True}

        with patch.object(self.native, 'preflight', side_effect=start_turn) as preflight:
            tick = self.engine.execute('tick')

        preflight.assert_called_once()
        self.assertEqual((tick['action'], tick['event']), ('capacity_held', 'periodic'))
        self.assertEqual(tick['capacity'], dict(maximum=1, available=0, occupied_states={'w1:p2': 'working'}))
        self.assertEqual(self.native.submissions, [])

    def test_periodic_submits_with_available_capacity(self):
        tick = self.engine.execute('tick')

        self.assertEqual(tick, dict(action='submitted', event='periodic', admission_id='accepted-1',
                                    capacity=dict(maximum=1, available=1, occupied_states={})))
        self.assertEqual(self.native.submissions, [{'type': 'periodic'}])

    def test_github_scheduler_can_run_in_a_separate_checkout_when_the_default_is_occupied(self):
        from workflows.github.scheduler import plan

        parked = dict(pane_id='w1:parked', terminal_id='parked-terminal', status='I:READ', user_locked=False)
        parked_status = dict(self.native.result, thread_id='parked-thread', turn_id='parked-turn')
        default = parked_status['working_directory']
        checkout = self.home / 'pr-26'
        checkout.mkdir()
        self.native.rows.append(parked)
        native_agent = self.native.agent

        def agent(operation, workspace, pane, *args, **kwargs):
            if pane == parked['pane_id']:
                self.assertEqual(operation, 'status')
                return dict(parked_status)
            return native_agent(operation, workspace, pane, *args, **kwargs)

        with patch.object(self.native, 'agent', side_effect=agent):
            capacity = self.engine.execute('capacity')
            self.assertEqual(capacity['available'], 1)
            self.assertEqual(capacity['workers']['worker']['hold'], 'directory_in_use')
            self.assertEqual(self.engine.execute('tick')['event'], 'periodic')
            self.assertEqual(self.native.submissions, [{'type': 'periodic'}])

            op, options = parse(f'capacity --worker worker --cwd {checkout}')
            checked = self.engine.execute(op, **options)
            self.assertEqual(checked['preflight'], dict(worker='worker', working_directory=str(checkout),
                                                        ready=True, hold=None))
            blocked = self.engine.execute('capacity', worker='worker', working_directory=default)
            self.assertEqual(blocked['preflight']['hold'], 'directory_in_use')
            self.native.quota_hold = True
            quota = self.engine.execute(op, **options)
            self.assertEqual(quota['preflight']['hold'], 'quota_behind_pace')
            self.native.quota_hold = False
            with patch.object(self.native, 'preflight', side_effect=Hold('worker_unavailable', 'Worker unavailable')):
                unavailable = self.engine.execute(op, **options)
                self.assertEqual(unavailable['preflight']['hold'], 'worker_unavailable')
            self.assertEqual(self.native.rows, [self.native.manager, parked])

            head = 'a' * 40
            handoff = dict(kind='review', pr=26, head=head, feedback_ids=[])
            pull = dict(number=26, headRefOid=head, state='OPEN', isDraft=False, mergeable='MERGEABLE',
                        autoMergeRequest=None, isInMergeQueue=False)
            entry = dict(issue=25, pr=26, pull=pull, eligible=True, placement='In review → Queue',
                         feedback='clear', pending_feedback=[], handoff=handoff)
            ids = dict(pane_id=parked['pane_id'], turn_id=parked_status['turn_id'])
            ack = self.engine.execute('mark_read', **ids)
            report = dict(ids, recorded=True, acknowledgment=dict(ok=True, result=ack))
            options = dict(implementer='unused', reviewer='worker')
            for event in ('periodic', 'worker_result'):
                with self.subTest(event=event):
                    request = dict(event=event, worker_result=report, entries=[entry], queued_at_start=[25],
                                   capacity=capacity)
                    prepared = plan(request, **options)
                    self.assertEqual(prepared['prepare_checkout']['worker'], 'worker')
                    self.assertIsNone(prepared['dispatch'])
                    request.update(entries=[dict(entry, handoff=dict(handoff, checkout=checked['preflight']))],
                                   capacity=checked)
                    dispatch = plan(request, **options)['dispatch']
                    self.assertEqual(dispatch['working_directory'], str(checkout))

            with self.assertRaises(Hold) as held:
                self.engine.execute('run', name='worker', working_directory=default, parameters={'TASK': 'review'})
            self.assertEqual((held.exception.code, held.exception.details['admission']),
                             ('directory_in_use', 'not_attempted'))
            receipt = self.engine.execute('run', name=dispatch['worker'], working_directory=dispatch['working_directory'],
                                          parameters={'TASK': 'review'})
            self.assertEqual(receipt['admission'], 'accepted')
            self.assertEqual(self.native.runs, 1)

    def test_result_is_delivered_while_another_worker_fills_capacity(self):
        busy = dict(self.native.worker, pane_id='w1:p3', terminal_id='t3')
        self.native.rows.extend([self.native.worker, busy])
        agent = self.native.agent

        def read_agent(operation, workspace, pane, *args, **kwargs):
            if pane == busy['pane_id']:
                self.assertEqual(operation, 'status')
                return dict(self.native.result, state='working', active=True,
                            thread_id='busy-thread', turn_id='busy-turn', working_directory=str(self.home))
            return agent(operation, workspace, pane, *args, **kwargs)

        with patch.object(self.native, 'agent', side_effect=read_agent):
            tick = self.engine.execute('tick')

        self.assertEqual(tick, dict(action='submitted', event='worker_result', admission_id='accepted-1',
                                    pane_id='w1:p2', turn_id='turn',
                                    capacity=dict(maximum=1, available=0, occupied_states={'w1:p3': 'working'})))
        self.assertEqual(self.native.submissions, [dict(type='worker_result', worker='worker',
                                                        pane_id='w1:p2', turn_id='turn', message='finished')])

    def test_capacity_refusal_allows_one_submission_when_the_slot_frees(self):
        self.native.rows.append(self.native.worker)
        self.native.result.update(state='working', active=True)
        self.assertEqual(self.engine.execute('capacity')['available'], 0)
        with self.assertRaises(Hold) as refused:
            self.engine.execute('run', name='worker', parameters={'TASK': 'next task'})
        self.assertEqual(refused.exception.code, 'capacity')
        self.assertEqual(self.engine.execute('capacity')['unresolved_launches'], [])
        self.assertEqual(self.native.runs, 0)

        self.native.rows.remove(self.native.worker)
        self.assertEqual(self.engine.execute('capacity')['available'], 1)
        admitted = self.engine.execute('run', name='worker', parameters={'TASK': 'next task'})
        self.assertEqual((admitted['admission'], admitted['pane_id'], admitted['turn_id']),
                         ('accepted', 'w1:p2', 'turn'))
        with self.assertRaises(Hold) as duplicate:
            self.engine.execute('run', name='worker', parameters={'TASK': 'next task'})
        self.assertEqual(duplicate.exception.code, 'already_started')
        self.assertEqual(duplicate.exception.details['turn_id'], admitted['turn_id'])
        self.assertEqual(self.native.runs, 1)

    def test_zoom_and_acknowledged_attention_use_a_slot(self):
        self.native.worker['user_locked'] = True
        self.native.rows.append(self.native.worker)
        self.assertEqual(self.engine.execute('capacity')['occupied'], ['w1:p2'])
        self.native.worker['user_locked'] = False
        self.native.result.update(state='blocked', active=True, attention=dict(
            attention_id='ask', state='blocked', reason='operator approval', observed_at='2026-11-26T07:00:00Z'))
        event = self.engine.execute('tick')
        self.assertEqual(event['event'], 'worker_attention')
        self.assertEqual(event['action'], 'submitted')
        self.assertEqual(event['attention_id'], 'ask')
        self.assertEqual(event['capacity'], dict(maximum=1, available=0, occupied_states={'w1:p2': 'blocked'}))
        self.assertEqual([submission['type'] for submission in self.native.submissions], ['worker_attention'])
        self.engine.execute('mark_read', pane_id='w1:p2', turn_id='turn', attention_id='ask')
        self.assertEqual(self.engine.execute('capacity')['occupied'], ['w1:p2'])
        self.assertEqual(self.engine.execute('tick')['action'], 'capacity_held')

    def test_quota_hold_and_verified_account_admission(self):
        self.native.quota_hold = True
        self.assertEqual(self.engine.execute('tick')['action'], 'quota_held')
        with self.assertRaisesRegex(Hold, 'quota holds'):
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.native.quota_hold = False
        self.assertEqual(self.engine.execute('run', name='worker', parameters={'TASK': 'test'})['pane_id'], 'w1:p2')
        self.assertEqual(self.native.worker['status'], 'I:RUN')

    def test_partial_provision_reports_no_submission_and_defers_cleanup(self):
        self.native.provision_error = Hold('partial_provision', 'Codex TUI attachment was not verified', pane_id='w1:p2')
        self.native.worker['agent_status'] = 'blocked'
        self.native.worker['creation_pending'] = True
        with self.assertRaises(Hold) as error:
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual(error.exception.as_dict(), dict(code='partial_provision',
            message='Codex TUI attachment was not verified', pane_id='w1:p2', workspace_id='w1',
            admission='not_attempted', phase='provision'))
        self.assertEqual(self.native.runs, 0)
        self.engine = Engine(self.home, self.native)
        row = self.engine.execute('query', pane_id='w1:p2')
        self.assertEqual((row['state'], row['admission'], row['thread_id']), ('starting', 'not_attempted', None))
        capacity = self.engine.execute('capacity')
        self.assertEqual(capacity['occupied'], ['w1:p2'])
        self.assertEqual(capacity['unresolved_launches'][0]['admission'], 'not_attempted')
        self.assertEqual(self.native.closes, [])
        hold = self.engine.execute('prepare_panes')['holds'][0]['error']
        self.assertEqual((hold['reason'], hold['dependency']),
                         ('turn_guard_unavailable', 'hermes-herdr-agent-manager'))
        for _ in range(2):
            tick = self.engine.execute('tick')
            self.assertEqual(tick['action'], 'capacity_held')
            self.assertEqual(tick['holds'][0]['error'], hold)
            self.assertEqual(tick['unresolved_launches'][0]['pane_id'], 'w1:p2')
        self.assertEqual(self.native.submissions, [])
        self.assertEqual(self.native.closes, [])
        self.assertEqual(self.engine.execute('capacity')['available'], 0)
        # Simulate the operator closing the pane directly in Herdr.
        self.native.rows.remove(self.native.worker)
        self.assertEqual(self.engine.execute('capacity')['available'], 1)
        self.assertEqual(self.engine.execute('tick')['event'], 'periodic')

    def test_provision_timeout_can_be_discovered_and_cleanup_waits_for_lock_and_binding(self):
        self.native.provision_error = Hold('transport_error', 'Provision reply was lost')
        with self.assertRaises(Hold) as error:
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual(error.exception.details['admission'], 'not_attempted')
        self.assertNotIn('pane_id', error.exception.details)
        for locked, effective in ((True, 'working'), (None, 'unknown')):
            self.native.worker['user_locked'] = locked
            panes = self.engine.execute('prepare_panes')['panes']
            self.assertEqual(panes[1]['state'], effective)
        self.native.worker['user_locked'] = False
        self.native.status_error = Hold('receiver_unavailable', 'No verified receiver')
        self.assertEqual(self.engine.execute('prepare_panes')['holds'][0]['pane_id'], 'w1:p2')
        self.assertEqual(self.native.closes, [])
        self.native.status_error = None
        self.native.worker['pending_closure'] = True
        result = self.engine.execute('prepare_panes')
        self.assertEqual(result['holds'][0]['error']['status_code'], 'close_deferred')
        self.assertEqual(self.engine.execute('close', pane_id='w1:p2')['reason'], 'turn_guard_unavailable')
        self.assertEqual(self.native.closes, [])
        self.assertEqual(self.engine.execute('capacity')['available'], 0)
        self.native.rows.remove(self.native.worker)
        self.assertEqual(self.engine.execute('capacity')['available'], 1)
        self.assertEqual(self.native.runs, 0)

    def test_refusal_cleanup_leaves_an_operator_turn_starting_after_the_idle_read(self):
        self.native.run_error = Hold('quota_account_changed', 'Account changed before turn admission')
        with self.assertRaises(Hold) as error:
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual(error.exception.code, 'quota_account_changed')
        self.assertEqual(error.exception.details['admission'], 'not_attempted')
        cleanup = error.exception.details['cleanup']
        self.assertEqual((cleanup['closed'], cleanup['reason'], cleanup['dependency']),
                         (False, 'turn_guard_unavailable', 'hermes-herdr-agent-manager'))
        self.assertEqual(self.native.closes, [])

        agent = self.native.agent

        def admit_operator_after_snapshot(operation, *args, **kwargs):
            reply = agent(operation, *args, **kwargs)
            if operation == 'status':
                self.native.result.update(state='working', active=True)
            return reply

        with patch.object(self.native, 'agent', side_effect=admit_operator_after_snapshot):
            holds = self.engine.execute('prepare_panes')['holds']
        self.assertEqual(holds[0]['error']['reason'], 'turn_guard_unavailable')
        self.assertEqual(self.native.result['state'], 'working')
        self.assertIn(self.native.worker, self.native.rows)
        self.assertEqual(self.native.worker['status'], 'I:START')
        self.assertEqual(self.native.closes, [])
        self.assertEqual(self.engine.execute('prepare_panes')['holds'][0]['error']['code'], 'worker_unavailable')
        self.assertEqual(self.engine.execute('capacity')['available'], 0)

    def test_codex_refusal_and_prewrite_deadline_allow_retry_after_restart(self):
        for index, code in enumerate(('codex_rejected', 'deadline_elapsed'), 2):
            with self.subTest(code=code):
                native = Dependencies(self.native.result['working_directory'])
                pane = f'w1:p{index}'
                native.worker.update(pane_id=pane, terminal_id=f't{index}')
                native.run_error = Hold(code, 'Definitive refusal from the receiver')
                request = dict(name='worker', parameters={'TASK': code})
                with self.assertRaises(Hold) as error:
                    Engine(self.home, native).execute('run', **request)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(error.exception.details['admission'], 'not_attempted')
                self.assertEqual(error.exception.details['pane_id'], pane)
                self.assertEqual(native.closes, [])
                with self.assertRaises(Hold) as held:
                    Engine(self.home, native).execute('run', **request)
                self.assertEqual(held.exception.code, 'capacity')
                self.assertEqual(native.runs, 1)
                native.rows.remove(native.worker)
                native.run_error = None
                engine = Engine(self.home, native)
                self.assertEqual(engine.execute('capacity')['available'], 1)
                self.assertEqual(engine.execute('run', **request)['admission'], 'accepted')
                self.assertEqual(native.runs, 2)

    def test_partial_write_and_missing_reply_deadlines_keep_the_prompt_on_hold(self):
        timeouts = (('outcome_unknown', 'Timed out after a partial prompt write'),
                    ('transport_error', 'Timed out waiting for the submission reply'))
        for index, (code, message) in enumerate(timeouts, 2):
            with self.subTest(code=code):
                native = Dependencies(self.native.result['working_directory'])
                native.worker.update(pane_id=f'w1:p{index}', terminal_id=f't{index}')
                native.run_error = Hold(code, message)
                request = dict(name='worker', parameters={'TASK': code})
                with self.assertRaises(Hold) as error:
                    Engine(self.home, native).execute('run', **request)
                self.assertEqual((error.exception.code, error.exception.details['cause']), ('outcome_unknown', code))
                self.assertEqual(native.closes, [])
                native.rows.remove(native.worker)
                with self.assertRaisesRegex(Hold, 'already has an unverified submission'):
                    Engine(self.home, native).execute('run', **request)
                self.assertEqual(native.runs, 1)

    def test_lost_reply_holds_ambiguous_attention_until_managed_turn_is_observed(self):
        self.native.run_error = Hold('transport_error', 'Run receipt was lost')
        with self.assertRaisesRegex(Hold, 'Run receipt was lost'):
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        attention = dict(attention_id='ask', state='blocked', reason='item/commandExecution/requestApproval',
                         observed_at='2026-11-26T07:00:00Z')
        self.native.result.pop('completed_at')
        for turn in ('operator-turn', 'turn'):
            with self.subTest(turn=turn):
                # The installed receiver returns this same shape for either origin.
                self.native.result.update(state='blocked', turn_id=turn, active=True, attention=attention)
                self.native.turn_results[turn] = dict(thread_id='thread', turn_id=turn, state='working')
                engine = Engine(self.home, self.native)
                row = engine.execute('query', pane_id='w1:p2')
                self.assertEqual((row['admission'], row['state'], row['delivery']), ('unknown', 'unknown', 'unknown'))
                self.assertEqual((row['reason']['code'], row['reason']['dependency']),
                                 ('managed_turn_identity_unavailable', 'hermes-herdr-agent-manager'))
                self.assertNotIn('turn_id', row)
                self.assertNotIn('attention', row)
                self.assertEqual(engine.execute('tick')['action'], 'capacity_held')
                with self.assertRaisesRegex(Hold, 'already has an unverified submission'):
                    engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual(self.native.submissions, [])

        # Outside attention status the receiver's turn ID comes from its managed admission.
        self.native.result.update(state='working')
        self.native.result.pop('attention')
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admitted_turn_id'], 'turn')
        self.native.result.update(state='blocked', turn_id='operator-turn', attention=attention)
        self.assertEqual(self.engine.execute('tick')['action'], 'capacity_held')
        self.assertEqual(self.native.submissions, [])
        self.native.result.update(turn_id='turn')
        event = self.engine.execute('tick')
        self.assertEqual((event['event'], event['turn_id'], event['attention_id']), ('worker_attention', 'turn', 'ask'))
        self.assertEqual(self.native.worker['status'], 'I:WAIT:ask')
        self.native.turn_results.clear()
        engine = Engine(self.home, self.native)
        row = engine.execute('query', pane_id='w1:p2')
        self.assertEqual((row['admission'], row['admitted_turn_id'], row['state']), ('accepted', 'turn', 'blocked'))
        engine.execute('mark_read', pane_id='w1:p2', turn_id='turn', attention_id='ask')
        self.assertEqual(engine.execute('capacity')['occupied'], ['w1:p2'])
        self.assertEqual(engine.execute('tick')['action'], 'capacity_held')
        self.assertEqual(self.native.runs, 1)

    def test_receipt_survives_label_refusal_and_excludes_a_different_attention_turn(self):
        self.native.rename_errors['I:RUN'] = Hold('user_locked', 'The operator zoomed before the label update')
        accepted = self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual((accepted['admission'], self.native.worker['status']), ('accepted', 'I:START'))
        engine = Engine(self.home, self.native)
        self.native.result.update(state='blocked', turn_id='operator-turn', active=True, attention=dict(
            attention_id='ask', state='blocked', reason='approval', observed_at='2026-11-26T07:00:00Z'))
        row = engine.execute('query', pane_id='w1:p2')
        self.assertEqual((row['admission'], row['admitted_turn_id'], row['state']), ('accepted', 'turn', 'unknown'))
        self.assertNotIn('turn_id', row)
        self.assertEqual(engine.execute('tick')['action'], 'capacity_held')
        self.native.result['turn_id'] = 'turn'
        self.native.worker['user_locked'] = True
        self.assertEqual(engine.execute('tick')['action'], 'capacity_held')
        self.assertEqual(self.native.submissions, [])
        self.native.worker['user_locked'] = False
        event = engine.execute('tick')
        self.assertEqual((event['event'], event['turn_id']), ('worker_attention', 'turn'))
        with self.assertRaises(Hold) as error:
            engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual((error.exception.code, error.exception.details['turn_id']), ('already_started', 'turn'))
        self.assertEqual(self.native.runs, 1)
        self.assertEqual(self.native.closes, [])

    def test_suspended_snapshot_can_reconcile_verified_managed_completion(self):
        self.native.run_error = Hold('transport_error', 'Run receipt was lost')
        with self.assertRaises(Hold):
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.native.result.update(state='suspended', turn_id='turn', active=False)
        result = dict(thread_id='thread', turn_id='turn', state='done', outcome='completed',
                      message='finished', completed_at=None, parameters={})
        parameters = {'TASK': 'test', 'PR': 'none'}
        for evidence in (result, dict(result, parameters=dict(parameters, TASK='different')),
                         dict(result, thread_id='other', parameters=parameters)):
            self.native.turn_results['turn'] = evidence
            self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admission'], 'unknown')
        self.native.turn_results['turn'] = dict(result, parameters=parameters)
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admitted_turn_id'], 'turn')
        self.native.result.update(state='done')
        self.assertEqual(Engine(self.home, self.native).execute('tick')['event'], 'worker_result')
        self.assertEqual(self.native.runs, 1)

    def test_lost_admission_is_held_until_the_original_thread_identifies_the_turn(self):
        self.native.run_error = Hold('transport_error', 'Run receipt was lost')
        with self.assertRaises(Hold) as error:
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual(error.exception.code, 'outcome_unknown')
        self.assertEqual(error.exception.details['admission'], 'unknown')
        self.engine = Engine(self.home, self.native)
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admission'], 'unknown')
        self.assertEqual(self.engine.execute('tick')['action'], 'capacity_held')
        self.native.result.update(state='blocked', turn_id='operator-turn', active=True)
        self.engine.execute('prepare_panes')
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admission'], 'unknown')
        self.native.result.update(state='done', turn_id='turn', thread_id='different', active=False)
        self.engine.execute('prepare_panes')
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admission'], 'unknown')
        with self.assertRaisesRegex(Hold, 'Resolve the worker submission'):
            self.engine.execute('close', pane_id='w1:p2')
        self.native.result.update(thread_id='thread', state='unknown', recoverable=True)
        self.native.worker['user_locked'] = True
        self.engine.execute('prepare_panes')
        self.assertEqual(self.native.recoveries, 0)
        self.native.worker['user_locked'] = False
        self.engine.execute('prepare_panes')
        self.assertEqual(self.native.recoveries, 1)
        self.native.result.update(state='done')
        self.engine.execute('prepare_panes')
        self.assertEqual(self.engine.execute('tick')['event'], 'worker_result')
        self.assertEqual(self.native.runs, 1)
        self.assertEqual(self.native.closes, [])

    def test_admission_receipt_must_identify_the_provisioned_thread(self):
        self.native.admitted_thread = 'different'
        with self.assertRaises(Hold) as error:
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.assertEqual(error.exception.code, 'outcome_unknown')
        self.assertEqual(self.native.worker['status'], 'I:START')
        self.assertEqual(self.engine.execute('capacity')['available'], 0)
        self.assertEqual(self.native.closes, [])

    def test_uncertain_prompt_cannot_be_replayed_even_if_its_pane_is_gone(self):
        self.native.run_error = Hold('local_error', 'Run receipt did not verify an admission or refusal')
        with self.assertRaisesRegex(Hold, 'Run receipt'):
            self.engine.execute('run', name='worker', parameters={'TASK': 'test'})
        self.native.rows.remove(self.native.worker)
        capacity = self.engine.execute('capacity')
        self.assertEqual(capacity['available'], 1)
        self.assertEqual(capacity['unresolved_launches'][0]['presence'], 'no')
        other = self.home / 'other'
        other.mkdir()
        with self.assertRaisesRegex(Hold, 'already has an unverified submission'):
            Engine(self.home, self.native).execute('run', name='worker', parameters={'TASK': 'test'},
                                                   working_directory=str(other))
        self.assertEqual(self.native.runs, 1)

    def test_legacy_run_with_no_turn_is_not_proof_of_no_admission(self):
        self.native.rows.append(self.native.worker)
        self.native.result.update(state='idle', turn_id=None, active=False)
        self.engine.execute('prepare_panes')
        self.assertEqual(self.engine.execute('query', pane_id='w1:p2')['admission'], 'unknown')
        self.assertEqual(self.engine.execute('capacity')['occupied'], ['w1:p2'])
        self.assertEqual(self.native.closes, [])

    def test_provision_labels_the_pane_before_the_attachment_check(self):
        native = Native(self.home)
        worker = Settings.read(self.home, raw_settings(self.home)).workers['worker']
        with patch.object(native, 'agent') as agent:
            native.provision('w1', worker, worker.cwd)
        self.assertEqual(agent.call_args.args[-2:], ('--status', 'I:START'))

    def test_adapter_preserves_partial_creation_but_requires_a_valid_mutation_receipt(self):
        native = Native(self.home)
        with patch('project_manager_engine.hermes_native.subprocess.run') as child:
            child.return_value = CompletedProcess([], 1, json.dumps(dict(ok=False, error=dict(
                code='partial_create', message='Pane was created', pane_id='w1:p2'))))
            with self.assertRaises(Hold) as error:
                native.invoke([])
            self.assertEqual((error.exception.code, error.exception.details['pane_id']), ('partial_provision', 'w1:p2'))
            layout = dict(workspace_id='w1', tabs=[])
            child.return_value = CompletedProcess([], 0, json.dumps(layout))
            self.assertEqual(native.invoke([], bare=True), layout)
            with self.assertRaisesRegex(Hold, 'no verified successful receipt'):
                native.invoke([])
            child.return_value = CompletedProcess([], 0, json.dumps(dict(ok=False, error=dict(
                code='deadline_elapsed', message='Timed out before writing'))))
            with self.assertRaisesRegex(Hold, 'invalid error receipt'):
                native.invoke([])
            child.side_effect = TimeoutExpired('agent', 80)
            with self.assertRaises(Hold) as error:
                native.invoke([])
            self.assertEqual(error.exception.code, 'transport_error')

    def test_quota_allows_equality(self):
        reset = '2026-11-26T00:10:00+00:00'
        instant = datetime.fromisoformat('2026-11-26T00:04:00+00:00').timestamp()
        window = dict(remaining_percent=60, window_minutes=10, reset_at=reset)
        self.assertFalse(quota_plan({'primary': window}, instant)[0]['hold'])
        self.assertTrue(quota_plan({'primary': dict(window, remaining_percent=59)}, instant)[0]['hold'])

    def test_command_preserves_literal_prompt_after_options(self):
        operation, args = parse("run worker --param \"TASK=hello there\" -- don't expand $PWD")
        self.assertEqual(operation, 'run')
        self.assertEqual(args['parameters'], {'TASK': 'hello there'})
        self.assertEqual(args['prompt'], "don't expand $PWD")
        self.assertEqual(parse('set-comment w1:p2 ""')[1]['comment'], '')
        with self.assertRaisesRegex(Hold, 'distinct'):
            parse('run worker --param TASK=x --param TASK=y')

    def test_only_the_bound_watchdogs_interrupted_admission_can_be_resolved(self):
        worker = {'ref': {'session_id': 'bound'}, 'status': 'unknown', 'execution_generation': 2,
                  'input_id': WATCHDOG_INPUT + 'request', 'admission_id': 'head'}
        operator = dict(worker, input_id='operator-input')
        self.assertIs(unknown_watchdog_admission({'running': False, 'pending': [worker]}, 'bound'), worker)
        self.assertIsNone(unknown_watchdog_admission({'running': True, 'pending': [worker]}, 'bound'))
        self.assertIsNone(unknown_watchdog_admission({'running': False, 'pending': [operator, worker]}, 'bound'))
        self.assertIsNone(unknown_watchdog_admission({'running': False, 'pending': [worker]}, 'other'))
