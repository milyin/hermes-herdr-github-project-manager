import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from project_manager_engine.settings import SKILL, Settings
from scripts.ensure_github_label import ensure_label


ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repository'
APP = 'example-app'
LABEL = 'hermes:projectmanager'


class BundledWorkflows(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'skills').mkdir()
        (self.home / 'skills' / SKILL).symlink_to(ROOT, target_is_directory=True)
        docs = (ROOT / 'doc' / 'PROFILE.md').read_text(encoding='utf-8')
        github, demo = map(yaml.safe_load, re.findall(r'```yaml\n(.*?)\n```', docs, re.DOTALL))
        self.github = github['skills']['config'][SKILL]
        self.demo = demo
        for config in (self.github, self.demo):
            for agent in (config['manager'], *config['workers']):
                agent['working_directory'] = str(self.home)

    def test_github_example_renders_scoped_commands_for_all_roles(self):
        self.github['manager']['parameters']['github_identity'] = APP
        self.github['manager']['parameters']['REPO'] = REPO
        settings = Settings.read(self.home, self.github)
        manager, _ = settings.manager.prompt.render()
        implementer, _ = settings.workers['implementer'].prompt.render(
            {'github_identity': APP, 'REPO': REPO, 'TASK': 'Implement the issue'})
        reviewer, _ = settings.workers['reviewer'].prompt.render(
            {'github_identity': APP, 'REPO': REPO, 'ISSUE': '6', 'PR': '7', 'HEAD': 'a' * 40})
        for prompt in (manager, implementer, reviewer):
            for action in ('status', 'gh', 'git'):
                self.assertIn(f'{APP} --repo {REPO} {action}', prompt)
            self.assertIn(f'{APP}[bot]', prompt)
            self.assertIn('Do not use personal accounts', prompt)
        self.assertIn(f'Pass `github_identity` as `{APP}` to both workers', manager)

    def test_demo_example_renders_without_github_configuration(self):
        settings = Settings.read(self.home, self.demo)
        manager, params = settings.manager.prompt.render()
        self.assertEqual(params, {})
        self.assertIn('run `hello`', manager)
        worker, params = settings.workers['hello'].prompt.render()
        self.assertEqual(params, {})
        self.assertIn('Hello from the project-manager demo!', worker)

    def test_startup_submits_interview_to_operator_once(self):
        spec = importlib.util.spec_from_file_location('project_manager_plugin_fixture', ROOT / '__init__.py',
                                                      submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: plugin,
                                     'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home)}):
            spec.loader.exec_module(plugin)
            ctx = Mock()
            with (patch.object(plugin, 'raw_settings', return_value={'enabled': True}),
                  patch.object(plugin, 'profile_context', side_effect=nullcontext),
                  patch.object(plugin, 'setup', side_effect=plugin.Hold('invalid_config', 'manager is required')),
                  patch.object(plugin, 'Native') as native,
                  patch.object(plugin.threading, 'Thread', side_effect=lambda target, **_: Mock(start=target)),
                  self.assertLogs(plugin.LOG, level='WARNING')):
                native.return_value.binding = {'session_id': 'bound'}
                plugin.register(ctx)
                hooks = dict(call.args for call in ctx.register_hook.call_args_list)
                hooks['on_session_open']('bound', 'tui')
                hooks['on_session_open']('operator', 'tui')
                hooks['on_session_start']('operator', 'tui')

            gateway = native.return_value.gateway
            gateway.assert_called_once()
            operation, session, message = gateway.call_args.args
            self.assertEqual((operation, session), ('submit', 'operator'))
            self.assertIn(str(ROOT / 'doc' / 'PROFILE.md'), message)
            self.assertIn(str(self.home / 'config.yaml'), message)
            self.assertIn('Start with the workflow choice', message)
            self.assertIn('GitHub App setup link', message)


class OptInLabel(unittest.TestCase):
    prefix = [APP, '--repo', REPO]
    endpoint = f'repos/{REPO}/labels'
    list_args = [*prefix, 'gh', 'api', endpoint, '--paginate', '--jq', '.[] | .name | @json']
    create_args = [*prefix, 'gh', 'api', endpoint, '--method', 'POST', '--raw-field', f'name={LABEL}',
                   '--raw-field', 'color=1d76db', '--raw-field', 'description=Opt in to project-manager automation']

    def setUp(self):
        run = patch('scripts.ensure_github_label.subprocess.run')
        self.run = run.start()
        self.addCleanup(run.stop)
        self.status = SimpleNamespace(stdout=json.dumps(
            {'authenticated': True, 'actor': f'{APP}[bot]', 'repository': REPO}))

    def commands(self):
        return [call.args[0] for call in self.run.call_args_list]

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
