import importlib.util
import json
from contextlib import ExitStack, nullcontext
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml

from project_manager_engine.settings import SKILL, Settings
from workflows.github.ensure_github_label import ensure_label


ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repository'
APP = '/absolute/path/to/profile/bin/example-app'
ACTOR = f'{Path(APP).name}[bot]'
LABEL = 'hermes:projectmanager'


class BundledWorkflows(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'skills').mkdir()
        (self.home / 'skills' / SKILL).symlink_to(ROOT, target_is_directory=True)
        def profile(workflow):
            guide = (ROOT / 'workflows' / workflow / 'README.md').read_text(encoding='utf-8')
            return yaml.safe_load(re.search(r'```yaml\n(.*?)\n```', guide, re.DOTALL)[1])

        self.github = profile('github')['skills']['config'][SKILL]
        self.demo = profile('hello-world')
        for config in (self.github, self.demo):
            for agent in (config['manager'], *config['workers']):
                agent['working_directory'] = str(self.home)

    def test_github_example_renders_scoped_commands_for_all_roles(self):
        launcher = self.github['manager']['parameters']['github_identity']
        self.assertTrue(Path(launcher).is_absolute())
        for worker in self.github['workers']:
            self.assertEqual(worker['parameters']['github_identity'], launcher)
        self.github['manager']['parameters']['REPO'] = REPO
        settings = Settings.read(self.home, self.github)
        manager, _ = settings.manager.prompt.render()
        implementer, _ = settings.workers['implementer'].prompt.render(
            {'REPO': REPO, 'TASK': 'Implement the issue'})
        reviewer, _ = settings.workers['reviewer'].prompt.render(
            {'REPO': REPO, 'ISSUE': '6', 'PR': '7', 'HEAD': 'a' * 40})
        for prompt in (manager, implementer, reviewer):
            for action in ('gh', 'git'):
                self.assertIn(f'{launcher} --repo {REPO} {action}', prompt)
            self.assertIn('Do not search PATH for a replacement', prompt)
            self.assertIn('Do not use personal accounts', prompt)
        self.assertIn(f'Pass `github_identity` as `{launcher}` to both workers', manager)

    def test_demo_example_renders_without_github_configuration(self):
        settings = Settings.read(self.home, self.demo)
        manager, params = settings.manager.prompt.render()
        self.assertEqual(params, {})
        self.assertIn('run `hello`', manager)
        worker, params = settings.workers['hello'].prompt.render()
        self.assertEqual(params, {})
        self.assertIn('Hello from the project-manager demo!', worker)


class StartupInterview(unittest.TestCase):
    class GatewayClientError(ValueError):
        pass

    class GatewayRPCError(GatewayClientError):
        pass

    NOT_SENT = 'Gateway disconnected before the request was sent; nothing was sent.'

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        spec = importlib.util.spec_from_file_location('project_manager_plugin_fixture', ROOT / '__init__.py',
                                                      submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.discovery = SimpleNamespace(state='ready', endpoint='fixture')
        self.rpc = AsyncMock()
        connection = AsyncMock()
        connection.__aenter__.return_value = SimpleNamespace(rpc=self.rpc)
        gateway_client = SimpleNamespace(GatewayClientError=self.GatewayClientError, GatewayRPCError=self.GatewayRPCError,
                                         connect_gateway=lambda _: connection, outside_restart_wait=nullcontext,
                                         NOT_SENT=self.NOT_SENT)
        gateway_runtime = SimpleNamespace(ensure_gateway_runtime=lambda *args, **kwargs: self.discovery,
                                          attachable=lambda _: True)
        stack.enter_context(patch.dict(sys.modules, {
            spec.name: plugin,
            'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home),
            'hermes_cli': SimpleNamespace(gateway_client=gateway_client, gateway_runtime=gateway_runtime),
            'hermes_cli.gateway_client': gateway_client,
            'hermes_cli.gateway_runtime': gateway_runtime,
        }))
        spec.loader.exec_module(plugin)
        stack.enter_context(patch.object(plugin, 'raw_settings', return_value={'enabled': True}))
        stack.enter_context(patch.object(plugin, 'profile_context', side_effect=nullcontext))
        stack.enter_context(patch.object(plugin, 'setup', side_effect=plugin.Hold('invalid_config', 'manager is required')))
        stack.enter_context(patch.object(plugin, 'threading', SimpleNamespace(
            Lock=plugin.threading.Lock, Thread=lambda target, **_: Mock(start=target))))
        self.logs = stack.enter_context(self.assertLogs(plugin.LOG, level='WARNING'))
        plugin.Native(self.home).save_binding({'workspace': 'w1', 'session_id': 'bound'})
        self.snapshot = {'session_id': 'operator', 'running': False, 'pending': []}
        self.receipt = {'ref': {'session_id': 'operator'}, 'admission_id': 'admitted'}
        self.rpc.side_effect = [self.snapshot, self.receipt]
        ctx = Mock()
        plugin.register(ctx)
        self.hooks = dict(call.args for call in ctx.register_hook.call_args_list)

    def methods(self):
        return [call.args[0] for call in self.rpc.await_args_list]

    def test_startup_submits_interview_to_operator_once(self):
        self.hooks['on_session_open']('bound', 'tui')
        self.hooks['on_session_open']('operator', 'tui')
        self.hooks['on_session_start']('operator', 'tui')

        self.assertEqual(self.methods(), ['session.resume', 'prompt.submit'])
        self.assertEqual(self.rpc.await_args.kwargs['session_id'], 'operator')
        message = self.rpc.await_args.kwargs['text']
        self.assertIn(str(ROOT / 'doc' / 'PROFILE.md'), message)
        self.assertIn(str(self.home / 'config.yaml'), message)
        self.assertIn('Start with the workflow choice', message)
        self.assertIn('creating a workflow from scratch', message)
        self.assertIn('Follow the selected workflow setup guide', message)
        self.assertNotIn('GitHub App', message)

    def test_startup_retries_definite_gateway_rejection(self):
        self.discovery.state = 'starting'
        self.hooks['on_session_open']('operator', 'tui')
        self.rpc.assert_not_awaited()
        self.discovery.state = 'ready'
        self.hooks['on_session_start']('operator', 'tui')
        self.hooks['on_session_open']('operator', 'tui')

        self.assertEqual(self.methods(), ['session.resume', 'prompt.submit'])
        self.assertIn('gateway_unavailable', '\n'.join(self.logs.output))

    def test_startup_retries_after_disconnect_during_resume(self):
        self.rpc.side_effect = [self.GatewayClientError('Connection lost'), self.snapshot, self.receipt]
        self.hooks['on_session_open']('operator', 'tui')
        self.assertEqual(self.methods(), ['session.resume'])
        self.hooks['on_session_start']('operator', 'tui')
        self.hooks['on_session_open']('operator', 'tui')

        self.assertEqual(self.methods(), ['session.resume', 'session.resume', 'prompt.submit'])
        self.assertIn('gateway_unavailable', '\n'.join(self.logs.output))

    def test_startup_retries_when_gateway_client_confirms_submit_was_not_sent(self):
        self.rpc.side_effect = [self.snapshot, self.GatewayClientError(self.NOT_SENT), self.snapshot, self.receipt]
        self.hooks['on_session_open']('operator', 'tui')
        self.hooks['on_session_start']('operator', 'tui')
        self.hooks['on_session_open']('operator', 'tui')

        self.assertEqual(self.methods(), ['session.resume', 'prompt.submit', 'session.resume', 'prompt.submit'])
        self.assertIn('gateway_unavailable', '\n'.join(self.logs.output))

    def test_startup_holds_duplicate_after_lost_submit_reply(self):
        self.rpc.side_effect = [self.snapshot, self.GatewayClientError('Reply lost'),
                                dict(self.snapshot, session_id='another-operator'),
                                dict(self.receipt, ref={'session_id': 'another-operator'})]
        self.hooks['on_session_open']('operator', 'tui')
        self.hooks['on_session_start']('operator', 'tui')
        self.hooks['on_session_open']('operator', 'tui')

        self.assertEqual(self.methods(), ['session.resume', 'prompt.submit'])
        self.assertIn('outcome_unknown', '\n'.join(self.logs.output))
        self.hooks['on_session_open']('another-operator', 'tui')
        self.assertEqual(self.methods(), ['session.resume', 'prompt.submit', 'session.resume', 'prompt.submit'])
        self.assertEqual(self.rpc.await_args.kwargs['session_id'], 'another-operator')


class OptInLabel(unittest.TestCase):
    prefix = [APP, '--repo', REPO]
    endpoint = f'repos/{REPO}/labels'
    list_args = [*prefix, 'gh', 'api', endpoint, '--paginate', '--jq', '.[] | .name | @json']
    create_args = [*prefix, 'gh', 'api', endpoint, '--method', 'POST', '--raw-field', f'name={LABEL}',
                   '--raw-field', 'color=1d76db', '--raw-field', 'description=Opt in to project-manager automation']

    def setUp(self):
        run = patch('workflows.github.ensure_github_label.subprocess.run')
        self.run = run.start()
        self.addCleanup(run.stop)
        self.status = SimpleNamespace(stdout=json.dumps({'authenticated': True, 'actor': ACTOR,
                                                        'repository': REPO, 'permissions': {'contents': 'write'}}))

    def commands(self):
        return [call.args[0] for call in self.run.call_args_list]

    def test_bare_app_slug_never_searches_path(self):
        cast(Mock, self.run).side_effect = [self.status, SimpleNamespace(stdout=json.dumps(LABEL))]
        with self.assertRaisesRegex(ValueError, 'absolute'):
            ensure_label(REPO, Path(APP).name, LABEL)
        self.assertEqual(self.commands(), [])

    def test_only_creates_a_missing_label(self):
        self.run.side_effect = [self.status, SimpleNamespace(stdout='"another label"\n'), SimpleNamespace(stdout='')]
        self.assertEqual(ensure_label(REPO, APP, LABEL), LABEL)
        self.assertEqual(self.commands(), [[*self.prefix, 'status'], self.list_args, self.create_args])

    def test_existing_label_preserves_spelling_without_a_write(self):
        self.run.side_effect = [self.status, SimpleNamespace(stdout=f'{json.dumps(LABEL.upper())}\n')]
        self.assertEqual(ensure_label(REPO, APP, LABEL), LABEL.upper())
        self.assertEqual(self.commands(), [[*self.prefix, 'status'], self.list_args])

    def test_wrong_app_stops_before_label_operations(self):
        self.run.return_value = SimpleNamespace(stdout=json.dumps(
            {'authenticated': True, 'actor': 'different-app[bot]', 'repository': REPO}))
        with self.assertRaisesRegex(ValueError, 'Cannot verify example-app'):
            ensure_label(REPO, APP, LABEL)
        self.assertEqual(self.commands(), [[*self.prefix, 'status']])

    def test_setup_requires_contents_write_before_label_operations(self):
        status = {'authenticated': True, 'actor': ACTOR, 'repository': REPO}
        for permissions in ({'contents': 'read', 'issues': 'write'}, {}):
            with self.subTest(permissions=permissions):
                self.run.reset_mock()
                self.run.return_value = SimpleNamespace(stdout=json.dumps(dict(status, permissions=permissions)))
                with self.assertRaisesRegex(ValueError, r'requires permissions\.contents: write'):
                    ensure_label(REPO, APP, LABEL)
                self.assertEqual(self.commands(), [[*self.prefix, 'status']])

    def test_failed_lookup_never_attempts_creation(self):
        self.run.side_effect = [self.status, subprocess.CalledProcessError(1, self.list_args)]
        with self.assertRaises(subprocess.CalledProcessError):
            ensure_label(REPO, APP, LABEL)
        self.assertEqual(self.commands(), [[*self.prefix, 'status'], self.list_args])

    def test_rechecks_after_an_uncertain_create(self):
        failure = subprocess.CalledProcessError(1, self.create_args)
        for found in (False, True):
            with self.subTest(found=found):
                self.run.reset_mock()
                self.run.side_effect = [self.status, SimpleNamespace(stdout=''), failure,
                                        SimpleNamespace(stdout=json.dumps(LABEL) if found else '')]
                if found:
                    self.assertEqual(ensure_label(REPO, APP, LABEL), LABEL)
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        ensure_label(REPO, APP, LABEL)
                self.assertEqual(self.commands(), [[*self.prefix, 'status'], self.list_args,
                                                   self.create_args, self.list_args])
