import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired, run
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

    def provision(self, *_):
        self.worker['status'] = 'I:START'
        self.rows.append(self.worker)
        self.result.update(state='idle', thread_id='thread', turn_id=None, active=False)
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
