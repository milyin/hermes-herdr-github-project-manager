"""Check the bundled launcher's scope with local configuration and mocked GitHub."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workflows.github import github_app
from workflows.github.github_app import (
    Config, GitHubError, Installation, cli_environment, git_command, github_command, main,
)
from workflows.github.install_proxy import install_proxy


APP = 'another-app'
REPO = 'other-owner/other-repository'
URL = f'https://github.com/{REPO}.git'


class AppLauncher(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / 'app settings.json'
        self.key = self.home / 'key.pem'
        self.key.write_text('fixture-key')
        self.key.chmod(0o600)
        self.path.write_text(json.dumps(dict(app_slug=APP, app_id=123, repository=REPO,
                                             private_key=str(self.key))))
        self.path.chmod(0o600)
        self.responses = {
            '/app': {'slug': APP, 'id': 123},
            f'/repos/{REPO}/installation': {'id': 22, 'app_id': 123, 'account': {'login': 'other-owner'}},
            '/app/installations/22/access_tokens': {'token': 'fixture-token', 'permissions': {'contents': 'write'},
                                                    'expires_at': '2026-11-26T10:00:00Z'},
            f'/repos/{REPO}': {'full_name': REPO},
            f'/users/{APP}%5Bbot%5D': {'login': APP + '[bot]', 'id': 777, 'type': 'Bot'},
        }

    def status(self, *, repo=REPO, launcher=APP):
        output, errors = StringIO(), StringIO()
        with (patch.object(sys, 'argv', [launcher]), redirect_stdout(output), redirect_stderr(errors),
              patch('workflows.github.github_app.jwt', return_value='fixture-jwt'),
              patch('workflows.github.github_app.request',
                    side_effect=lambda method, path, token, data=None: (self.responses[path], '')) as http):
            result = main(['--config', str(self.path), '--repo', repo, 'status'])
        return result, output.getvalue(), errors.getvalue(), http

    def test_dispatches_by_operator_chosen_app_and_scope(self):
        result, stdout, stderr, http = self.status()
        self.assertEqual(result, 0, stderr)
        self.assertEqual(json.loads(stdout)['actor'], APP + '[bot]')
        self.assertNotIn('fixture-token', stdout)
        self.assertEqual(http.call_args_list[2].args, (
            'POST', '/app/installations/22/access_tokens', 'fixture-jwt', {'repositories': ['other-repository']},
        ))

    def test_refuses_configured_scope_and_app_mismatch_before_network(self):
        for config in ({'repo': 'other-owner/different-repository'}, {'launcher': 'wrong-app'}):
            with self.subTest(config=config):
                result, stdout, _, http = self.status(**config)
                self.assertEqual((result, stdout), (1, ''))
                http.assert_not_called()

    def test_refuses_remote_identity_or_repository_mismatch(self):
        for path, response, count in (
            ('/app', {'slug': 'unexpected-app', 'id': 123}, 1),
            (f'/repos/{REPO}', {'full_name': 'other-owner/different-repository'}, 4),
        ):
            with self.subTest(path=path), patch.dict(self.responses, {path: response}):
                result, stdout, _, http = self.status()
                self.assertEqual((result, stdout), (1, ''))
                self.assertEqual(http.call_count, count)

    def test_refuses_an_unprotected_configuration_file(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private files'):
            Config.read(self.path, REPO, APP)

    def test_proxy_runs_the_bundled_launcher_and_rejects_config_overrides(self):
        proxy = install_proxy(self.path, self.home / 'local bin')
        self.assertEqual(proxy.name, APP)
        self.assertEqual(proxy.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(self.key.read_text(), proxy.read_text())
        result = subprocess.run([str(proxy), '--repo', REPO, 'merge-reviewed', '--help'],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertIn('--reviewed-head', result.stdout)
        result = subprocess.run([str(proxy), '--repo', REPO, '--config', str(self.path), 'status'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_proxy_uses_the_same_private_configuration_in_different_xdg_environments(self):
        proxy = install_proxy(self.path, self.home / 'bin')
        for role in ('manager', 'worker'):
            with self.subTest(role=role):
                output, errors = StringIO(), StringIO()
                with (patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.home / role)}),
                      patch.dict(sys.modules, {'github_app': github_app}),
                      patch.object(sys, 'path', sys.path.copy()),
                      patch.object(sys, 'argv', [str(proxy), '--repo', REPO, 'status']),
                      redirect_stdout(output), redirect_stderr(errors),
                      patch.object(github_app, 'jwt', return_value='fixture-jwt') as sign,
                      patch.object(github_app, 'request', side_effect=lambda method, path, token, data=None:
                                   (self.responses[path], '')), self.assertRaises(SystemExit) as finished):
                    runpy.run_path(str(proxy), run_name='__main__')
                self.assertEqual(finished.exception.code, 0, errors.getvalue())
                self.assertEqual(json.loads(output.getvalue())['actor'], APP + '[bot]')
                self.assertNotIn('fixture-token', output.getvalue())
                self.assertEqual(errors.getvalue(), '')
                sign.assert_called_once_with(Config(APP, '123', REPO, self.key))

    def test_installing_a_proxy_preserves_existing_commands_and_private_file_checks(self):
        proxy = install_proxy(self.path, self.home / 'bin')
        original = proxy.read_text()
        with self.assertRaisesRegex(GitHubError, 'already exists'):
            install_proxy(self.path, proxy.parent)
        self.assertEqual(proxy.read_text(), original)
        self.key.chmod(0o644)
        with self.assertRaisesRegex(GitHubError, 'private files'):
            install_proxy(self.path, self.home / 'other-bin')

    def test_installs_distinct_commands_for_distinct_app_configs(self):
        second = self.home / 'second.json'
        settings = json.loads(self.path.read_text())
        second.write_text(json.dumps(settings | {'app_slug': 'second-app', 'app_id': 456}))
        second.chmod(0o600)
        bin_dir = self.home / 'bin'
        first_proxy = install_proxy(self.path, bin_dir)
        second_proxy = install_proxy(second, bin_dir)
        self.assertEqual({entry.name for entry in bin_dir.iterdir()}, {APP, 'second-app'})
        self.assertIn(repr(str(self.path)), first_proxy.read_text())
        self.assertIn(repr(str(second)), second_proxy.read_text())

    def test_native_gh_runs_with_scoped_credentials_and_scrubs_the_personal_environment(self):
        with (patch.object(sys, 'argv', [APP]),
              patch.dict(os.environ, {'GH_TOKEN': 'personal-token', 'GITHUB_TOKEN': 'personal-token'}),
              patch('workflows.github.github_app.jwt', return_value='fixture-jwt'),
              patch('workflows.github.github_app.request',
                    side_effect=lambda method, path, token, data=None: (self.responses[path], '')),
              patch('workflows.github.github_app.subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0)) as native):
            self.assertEqual(main(['--config', str(self.path), '--repo', REPO,
                                   'gh', 'issue', 'list', '--label', 'opt-in']), 0)
        self.assertEqual(native.call_args.args[0], ['gh', 'issue', 'list', '--label', 'opt-in', '--repo', REPO])
        env = native.call_args.kwargs['env']
        self.assertEqual(env['GH_TOKEN'], 'fixture-token')
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotEqual(env['HOME'], str(Path.home()))

    def test_native_gh_cannot_bypass_checked_merge_or_repository_scope(self):
        self.assertEqual(github_command(REPO, ['pr', 'view', '9'])[-2:], ['--repo', REPO])
        self.assertEqual(github_command(REPO, ['api', f'repos/{REPO}/issues']), [
            'gh', 'api', f'repos/{REPO}/issues',
        ])
        for args in (
            ['pr', 'merge', '9'],
            ['api', f'repos/{REPO}/pulls/9/%6derge', '--method', 'PUT'],
            ['api', f'https://example.invalid/repos/{REPO}/issues'],
            ['api', 'repos/other-owner/unrelated/issues'],
            ['api', 'graphql'],
            ['pr', 'view', '9', '--repo', 'other-owner/unrelated'],
            ['pr', 'view', 'https://github.com/other-owner/unrelated/pull/9'],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                github_command(REPO, args)

    def test_git_uses_app_transport_only_for_the_repository_and_wiki(self):
        installation = Installation(REPO, APP + '[bot]', 777, 'fixture-token', {}, '')
        env = cli_environment(str(self.home))
        self.assertEqual(git_command(installation, ['status'], env)[-1], 'status')
        self.assertNotIn('GIT_CONFIG_VALUE_0', env)
        for url in (URL, URL.removesuffix('.git') + '.wiki.git'):
            with self.subTest(url=url), patch('workflows.github.github_app.check_git_config'):
                env = cli_environment(str(self.home))
                command = git_command(installation, ['fetch', url, 'refs/pull/9/head'], env)
                self.assertEqual(command[-2:], [url, 'refs/pull/9/head'])
                self.assertEqual(env['GIT_CONFIG_KEY_0'], f'http.{url}.extraHeader')
                self.assertTrue(env['GIT_CONFIG_VALUE_0'].startswith('Authorization: Basic '))
                self.assertFalse(any('fixture-token' in arg for arg in command))
        for args in (
            ['clone', 'https://github.com/other-owner/unrelated.git'],
            ['fetch', 'origin'],
            ['clone', '--config', URL],
            ['push', URL],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                git_command(installation, args, cli_environment(str(self.home)))

    def test_pages_rejects_invalid_and_incomplete_review_responses(self):
        installation = Installation(REPO, APP + '[bot]', 777, 'fixture-token', {}, '')
        for evidence in (({}, ''), ([], '<next>; rel="next"')):
            with self.subTest(evidence=evidence), patch('workflows.github.github_app.request', return_value=evidence):
                with self.assertRaises(ValueError):
                    list(installation.pages('pulls/9/reviews'))
        with patch('workflows.github.github_app.request', side_effect=GitHubError('Unavailable')):
            with self.assertRaises(GitHubError):
                list(installation.pages('pulls/9/reviews'))

    def test_pages_accepts_an_empty_first_page_without_a_next_link(self):
        installation = Installation(REPO, APP + '[bot]', 777, 'fixture-token', {}, '')
        with patch('workflows.github.github_app.request', return_value=([], '')) as http:
            self.assertEqual(list(installation.pages('pulls/9/reviews')), [])
        http.assert_called_once_with('GET', f'/repos/{REPO}/pulls/9/reviews?per_page=100&page=1', 'fixture-token')
