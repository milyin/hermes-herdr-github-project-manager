import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import ANY, Mock

import yaml

from project_manager_engine.engine import Engine, quota_plan
from project_manager_engine.hermes_native import WATCHDOG_INPUT, unknown_watchdog_admission
from project_manager_engine.interface import parse
from project_manager_engine.settings import Hold, SKILL, Settings, raw_settings


class Dependencies:
    binding = dict(workspace='w1', session_id='manager')

    def __init__(self, cwd):
        self.manager = dict(pane_id='w1:p1', status='MGR', user_locked=False)
        self.worker = dict(pane_id='w1:p2', status='I:RUN', user_locked=False)
        self.rows = [self.manager]
        self.manager_state = dict(attached=True, state='idle', pending=[])
        self.result = dict(worker='worker', agent='codex', thread_id='thread', turn_id='turn', state='done',
                           working_directory=cwd, completed_at='2026-11-26T07:00:00+00:00')
        self.agent_reads = 0
        self.submissions = []
        self.run_parameters = {'TASK': 'test'}
        self.run_receipts = []
        self.steered = []
        self.quota_hold = False

    def configure(self, settings):
        self.settings = settings

    def pane(self, operation, *args, workspace=None):
        if operation == 'workspaces':
            return {'workspaces': [{'workspace_id': 'w1', 'label': 'test'}]}
        if operation == 'layout':
            return dict(workspace_id=workspace, tabs=[dict(zoomed=False, panes=self.rows)])
        if operation == 'rename':
            row = next(pane for pane in self.rows if pane['pane_id'] == args[1])
            row[args[2][2:]] = args[3]
            return dict(row)
        if operation == 'close':
            self.rows.remove(self.worker)
            return {'closed': True}
        raise AssertionError(operation)

    def manager_status(self, *_):
        return dict(self.manager_state)

    def prepare_manager(self, *_):
        return self.manager

    def agent(self, operation, _workspace, pane, *args, payload=None):
        assert pane == self.worker['pane_id']
        self.agent_reads += 1
        if operation == 'status':
            return dict(self.result)
        if operation == 'result':
            assert args == ('--turn-id', 'turn')
            return dict(self.result, message='finished', parameters=self.run_parameters)
        if operation == 'run':
            assert payload['account'] == 'account'
            self.run_parameters = payload['parameters']
            self.run_receipts.append(payload.get('setup_receipt'))
            self.result.update(state='done', turn_id='turn')
            return dict(status='accepted', thread_id='thread', turn_id='turn')
        if operation == 'steer':
            self.steered.append(payload)
            return dict(status='accepted', thread_id='thread', turn_id='next-turn')
        raise AssertionError(operation)

    def quota(self, _):
        reset = datetime.now(timezone.utc) + timedelta(hours=1)
        return dict(account='account', ordinary_usage_allowed=True, primary=dict(
            remaining_percent=0 if self.quota_hold else 100, window_minutes=120, reset_at=reset.isoformat()))

    def preflight(self, *_):
        return {'ready': True}

    def provision(self, *_):
        self.rows.append(self.worker)
        self.result.update(state='idle', turn_id=None)
        return self.worker

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

    def configure_hook(self):
        hook = self.home / 'setup.py'
        hook.touch()
        self.config['setup_hook'] = hook.name
        for name in ('manager', 'worker'):
            prompt = self.home / f'{name}.md'
            prompt.write_text(prompt.read_text() + ' using {{setup_command}}')
        self.save()
        self.native.setup_environment = Mock(return_value=({'setup_command': '/fixture/command'}, 'setup-receipt'))

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

    def test_zoom_and_acknowledged_attention_use_a_slot(self):
        self.native.worker['user_locked'] = True
        self.native.rows.append(self.native.worker)
        self.assertEqual(self.engine.execute('capacity')['occupied'], ['w1:p2'])
        self.native.worker['user_locked'] = False
        self.native.result.update(state='blocked', active=True, attention=dict(
            attention_id='ask', state='blocked', reason='operator approval', observed_at='2026-11-26T07:00:00Z'))
        event = self.engine.execute('tick')
        self.assertEqual(event['event'], 'worker_attention')
        self.assertEqual(event['attention_id'], 'ask')
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

    def test_manager_setup_requires_the_bound_session_before_admission(self):
        self.configure_hook()
        self.native.setup_environment.side_effect = Hold('setup_hook_failed', 'Fixture unavailable')
        self.engine.execute('prepare_panes')
        with self.assertRaisesRegex(Hold, 'Fixture unavailable'):
            self.engine.execute('tick')
        self.assertEqual(self.native.submissions, [])
        self.native.setup_environment.side_effect = None
        self.native.submit = Mock(return_value={'admission_id': 'manager-turn'})
        self.assertEqual(self.engine.execute('tick')['admission_id'], 'manager-turn')
        self.assertIn('workflow value using /fixture/command', self.native.submit.call_args.args[1])
        self.native.setup_environment.assert_called_with(self.native.settings.manager, str(self.home), {'KEY': 'value'},
                                                              target=ANY, workspace='w1')
        self.assertEqual(self.native.submit.call_args.kwargs, {'setup_receipt': 'setup-receipt'})

    def test_worker_setup_uses_run_inputs_and_gates_followups(self):
        self.configure_hook()
        cwd = self.home / 'task checkout'
        cwd.mkdir()
        self.native.result['working_directory'] = str(cwd)
        run = dict(name='worker', parameters={'TASK': 'literal {{PR}}'}, working_directory=str(cwd))
        self.native.setup_environment.side_effect = Hold('setup_hook_failed', 'Fixture unavailable')
        with self.assertRaisesRegex(Hold, 'Fixture unavailable'):
            self.engine.execute('run', **run)
        self.assertEqual(self.native.rows, [self.native.manager])
        self.native.setup_environment.side_effect = None
        started = self.engine.execute('run', **run)
        parameters = {'TASK': 'literal {{PR}}', 'PR': 'none'}
        self.native.setup_environment.assert_called_with(self.native.settings.workers['worker'], str(cwd), parameters,
                                                              target=ANY, workspace='w1')
        self.assertEqual(self.native.setup_environment.call_args.kwargs['target']['thread_id'], 'thread')
        self.assertEqual(self.native.run_parameters, parameters | {'setup_command': '/fixture/command'})
        self.assertEqual(self.native.run_receipts, ['setup-receipt'])
        followup = dict(pane_id=started['pane_id'], prompt='literal {{setup_command}}')
        self.native.setup_environment.return_value = ({'setup_command': '/other/command'}, 'other-receipt')
        with self.assertRaisesRegex(Hold, 'worker environment changed'):
            self.engine.execute('steer', **followup)
        self.assertEqual(self.native.steered, [])
        self.native.setup_environment.return_value = ({'setup_command': '/fixture/command'}, 'followup-receipt')
        self.assertEqual(self.engine.execute('steer', **followup)['turn_id'], 'next-turn')
        self.assertEqual(self.native.steered, [dict(prompt=followup['prompt'], account='account',
                                                  setup_receipt='followup-receipt')])

    def test_followup_holds_when_current_hook_removes_a_saved_key(self):
        self.configure_hook()
        started = self.engine.execute('run', name='worker', parameters={'TASK': 'first'})
        (self.home / 'worker.md').write_text('start {{TASK}} with {{PR}}')
        self.native.setup_environment.return_value = ({}, 'new-receipt')
        with self.assertRaisesRegex(Hold, 'worker environment changed'):
            self.engine.execute('steer', pane_id=started['pane_id'], prompt='follow up')
        self.assertEqual(self.native.setup_environment.call_args.args[2], {'TASK': 'first', 'PR': 'none'})
        self.assertEqual(self.native.steered, [])

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
