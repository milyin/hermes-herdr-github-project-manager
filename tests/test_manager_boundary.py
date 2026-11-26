from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml

from project_manager_engine.settings import SKILL, Settings


ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repository'
APP = 'example-app'
SID = 'manager-session'
HEAD = 'a' * 40
WIKI_URL = f'https://github.com/{REPO}.wiki.git'
WIKI_REF = 'refs/heads/wiki-default'
PAGE = 'Project-Manager.md'
GIT = subprocess.run


class ManagerBoundary(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        spec = importlib.util.spec_from_file_location('project_manager_boundary_fixture', ROOT / '__init__.py',
                                                     submodule_search_locations=[str(ROOT)])
        self.plugin = importlib.util.module_from_spec(spec)
        modules = patch.dict(sys.modules, {
            spec.name: self.plugin,
            'hermes_constants': SimpleNamespace(get_hermes_home=lambda: self.home),
        })
        modules.start()
        self.addCleanup(modules.stop)
        spec.loader.exec_module(self.plugin)
        self.raw = dict(enabled=True, max_working_panes=1, workers=[], manager=dict(
            name='manager', model='fixture', description='Fixture manager',
            working_directory=str(self.home), prompt_file=str(ROOT / 'doc' / 'MANAGER_PROMPT.md'),
            parameters=dict(REPO=REPO, github_identity=APP, LABEL='opt-in', IMPLEMENTER='implementer',
                            REVIEWER='reviewer', human_reviewers='reviewer', wiki_page=PAGE)))
        (self.home / 'config.yaml').write_text(yaml.safe_dump(
            {'skills': {'config': {SKILL: self.raw}}}), encoding='utf-8')
        self.native = self.plugin.Native(self.home)
        self.native.save_binding({'workspace': 'w1', 'session_id': SID, 'github_scoped': True})
        ctx = Mock()
        self.plugin.register(ctx)
        self.handlers = {call.args[0]: (call.args[1], call.args[3]) for call in ctx.register_tool.call_args_list}
        self.filter = dict(call.args for call in ctx.register_middleware.call_args_list)['llm_request']
        self.gate = dict(call.args for call in ctx.register_hook.call_args_list)['pre_tool_call']
        self.app = Mock(side_effect=AssertionError('Unrecognized process call from manager'))
        mock = patch(f'{spec.name}.project_manager_engine.github_app.subprocess.run', self.app)
        mock.start()
        self.addCleanup(mock.stop)

    def invoke(self, name, args):
        block = self.gate(tool_name=name, args=args, session_id=SID)
        if block:
            return block
        return json.loads(self.handlers[name][1](args, session_id=SID))

    def capable_info(self):
        return dict(launch_request={'toolsets': [self.plugin.TOOLSET]}, enabled_toolsets=[self.plugin.TOOLSET],
                    tools={self.plugin.TOOLSET: sorted(self.plugin.SAFE_TOOLS)})

    def test_restricts_advertised_and_executable_manager_surface(self):
        terminal = 'terminal'
        execute = 'execute_code'
        relay = 'project_manager_steer'
        issue_read = 'project_manager_github_issues'
        wiki_write = 'project_manager_wiki_update'
        names = [terminal, execute, relay, issue_read, wiki_write, 'tool_call']
        schemas = [dict(type='function', function={'name': name}) for name in names]
        filtered = self.filter({'tools': schemas}, session_id=SID)['request']['tools']
        self.assertEqual([schema['function']['name'] for schema in filtered], [issue_read, wiki_write, 'tool_call'])
        self.assertEqual({name for name, (toolset, _) in self.handlers.items() if toolset == self.plugin.TOOLSET},
                         self.plugin.SAFE_TOOLS)
        self.assertNotEqual(self.handlers[relay][0], self.plugin.TOOLSET)
        self.assertIsNone(self.gate(tool_name=terminal, args={}, session_id='operator'))
        self.assertEqual(json.loads(self.handlers[relay][1]({'pane_id': 'w1:p2', 'prompt': 'merge PR #14'},
                                                            session_id=SID))['error']['code'], 'manager_tool_denied')
        self.assertIsNone(self.gate(tool_name='tool_call', args={'calls': [{'name': issue_read, 'arguments': {}}]},
                                    session_id=SID))
        self.app.assert_not_called()

    def test_denies_merge_auto_merge_queue_and_target_branch_writes(self):
        for command in (
            f'{APP} --repo {REPO} gh pr merge 14',
            f'{APP} --repo {REPO} gh pr merge 14 --auto',
            f'{APP} --repo {REPO} gh api --method PUT repos/{REPO}/pulls/14/merge',
            f'{APP} --repo {REPO} gh api graphql -f query="mutation {{ enablePullRequestAutoMerge(input: {{}}) {{ clientMutationId }} }}"',
            f'{APP} --repo {REPO} gh api graphql -f query="mutation {{ enqueuePullRequest(input: {{}}) {{ clientMutationId }} }}"',
            f'{APP} --repo {REPO} gh api --method PATCH repos/{REPO}/git/refs/heads/main',
            f'{APP} --repo {REPO} git push origin HEAD:refs/heads/main',
        ):
            with self.subTest(command=command):
                args = {'command': command}
                self.assertEqual(self.invoke('terminal', args)['action'], 'block')
                call = {'name': 'terminal', 'arguments': args}
                self.assertEqual(self.invoke('tool_call', {'calls': [call]})['action'], 'block')
        for name in ('execute_code', 'browser_navigate', 'project_manager_run', 'project_manager_steer',
                     'mcp_github_merge_pull_request', 'mcp_github_update_ref'):
            self.assertEqual(self.invoke(name, {})['action'], 'block', name)
        for extra in ({'ref': 'refs/heads/main'}, {'remote': f'https://github.com/{REPO}.git'},
                      {'page': '../.git/config'}, {'method': 'PUT'}, {'operation': 'merge'}):
            result = self.invoke('project_manager_wiki_update', dict(revision=HEAD, markdown='# Dashboard', **extra))
            self.assertEqual(result['error']['code'], 'invalid_arguments')
        for name in ('project_manager_github_pull', 'project_manager_github_handoff'):
            result = self.invoke(name, {'pr': 14, 'reviewed_head': HEAD, 'method': 'PUT'})
            self.assertFalse(result['ok'])
        self.app.assert_not_called()

    def test_legacy_session_cannot_dispatch_even_the_wiki_tool(self):
        self.native.save_binding({'workspace': 'w1', 'session_id': SID})
        arguments = dict(revision=HEAD, markdown='old manager')
        self.assertEqual(self.invoke('project_manager_wiki_update', arguments)['action'], 'block')
        handler = self.handlers['project_manager_wiki_update'][1]
        self.assertEqual(json.loads(handler(arguments, session_id=SID))['error']['code'],
                         'manager_boundary_unverified')
        self.app.assert_not_called()

    def test_scoped_issue_read_uses_only_the_configured_repo_and_label(self):
        status = dict(authenticated=True, actor=f'{APP}[bot]', repository=REPO)
        self.app.side_effect = [SimpleNamespace(stdout=json.dumps(status)),
                                SimpleNamespace(stdout=json.dumps([{'number': 14}]))]
        result = self.invoke('project_manager_github_issues', {})
        self.assertEqual(result, {'ok': True, 'result': {'issues': [{'number': 14}]}})
        calls = [call.args[0] for call in self.app.call_args_list]
        self.assertEqual(calls, [[APP, '--repo', REPO, 'status'],
                                 [APP, '--repo', REPO, 'gh', 'issue', 'list', '--repo', REPO, '--state', 'open',
                                  '--label=opt-in', '--limit', '100', '--json', 'number,title,url']])

    def test_gateway_pins_toolset_and_holds_before_submitting_to_unverified_session(self):
        self.native.configure(Settings.read(self.home, self.raw))
        rpc = AsyncMock()
        connection = AsyncMock()
        connection.__aenter__.return_value = SimpleNamespace(rpc=rpc)

        class GatewayClientError(RuntimeError):
            pass

        client = SimpleNamespace(GatewayClientError=GatewayClientError, GatewayRPCError=GatewayClientError,
                                 connect_gateway=lambda _: connection, outside_restart_wait=nullcontext)
        runtime = SimpleNamespace(ensure_gateway_runtime=lambda *args, **kwargs: SimpleNamespace(
            state='ready', endpoint='fixture'), attachable=lambda _: True)
        with patch.dict(sys.modules, {'hermes_cli': SimpleNamespace(gateway_client=client, gateway_runtime=runtime),
                                      'hermes_cli.gateway_client': client, 'hermes_cli.gateway_runtime': runtime}):
            rpc.return_value = {'session_id': SID}
            self.native.gateway('create', 'fixture')
            self.assertEqual(rpc.await_args.args, ('session.create',))
            self.assertEqual(rpc.await_args.kwargs['toolsets'], [self.plugin.TOOLSET])
            snapshot = dict(session_id=SID, running=False, pending=[], info=self.capable_info())
            for invalid in ({}, dict(self.capable_info(), launch_request={}),
                            dict(self.capable_info(), tools={'terminal': ['terminal']}),
                            dict(self.capable_info(), enabled_toolsets=[self.plugin.TOOLSET, 'terminal'])):
                rpc.reset_mock()
                rpc.return_value = dict(snapshot, info=invalid)
                with self.assertRaisesRegex(self.plugin.Hold, 'requires a verified scoped Hermes session'):
                    self.native.gateway('submit', SID, 'report')
                self.assertEqual([call.args[0] for call in rpc.await_args_list], ['session.resume'])
            rpc.reset_mock()
            rpc.side_effect = [snapshot, {'ref': {'session_id': SID}, 'admission_id': 'test'}]
            self.assertEqual(self.native.gateway('submit', SID, 'report')['admission_id'], 'test')
            self.assertEqual([call.args[0] for call in rpc.await_args_list], ['session.resume', 'prompt.submit'])

    def local_git(self, *args, **kwargs):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
                   GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        return GIT(['git', *map(str, args)], capture_output=True, check=True, env=env, **kwargs).stdout

    def test_wiki_update_changes_only_one_page_of_the_wikis_default_branch(self):
        remote = self.home / 'wiki.git'
        seed = self.home / 'seed'
        self.local_git('init', '--bare', '--initial-branch=wiki-default', remote)
        self.local_git('init', '--initial-branch=wiki-default', seed)
        (seed / PAGE).write_text('# Old dashboard\n', encoding='utf-8')
        (seed / 'Other.md').write_text('# Other page\n', encoding='utf-8')
        self.local_git('-C', seed, 'add', PAGE, 'Other.md')
        self.local_git('-C', seed, 'commit', '-m', 'Initialize wiki')
        self.local_git('-C', seed, 'push', remote, WIKI_REF)
        before = self.local_git('-C', remote, 'rev-parse', WIKI_REF).decode().strip()
        pushes = []

        def app(command, **kwargs):
            self.assertEqual(command[:3], [APP, '--repo', REPO])
            if command[3:] == ['status']:
                return SimpleNamespace(stdout=json.dumps(dict(authenticated=True, actor=f'{APP}[bot]',
                                                               repository=REPO, permissions={'contents': 'write'})))
            self.assertEqual(command[3:5], ['git', '-C'])
            args = command[4:]
            if args[2] == 'push':
                pushes.append(args[2:])
            for arg in args[2:]:
                if '://' in arg:
                    self.assertEqual(arg, WIKI_URL)
            return SimpleNamespace(stdout=self.local_git(*(str(remote) if arg == WIKI_URL else arg for arg in args),
                                                         input=kwargs.get('input')))

        self.app.side_effect = app
        read = self.invoke('project_manager_wiki_read', {})['result']
        self.assertEqual(read, dict(revision=before, markdown='# Old dashboard\n'))
        self.assertEqual(pushes, [])
        markdown = '## Need attention\n\nAwaiting human approval\n'
        result = self.invoke('project_manager_wiki_update', dict(revision=before, markdown=markdown))
        self.assertTrue(result['ok'], result)
        after = result['result']['revision']
        self.assertEqual(pushes, [['push', '--recurse-submodules=no', f'--force-with-lease={WIKI_REF}:{before}',
                                   WIKI_URL, f'{after}:{WIKI_REF}']])
        self.assertEqual(self.local_git('-C', remote, 'rev-parse', WIKI_REF).decode().strip(), after)
        self.assertEqual(self.local_git('-C', remote, 'diff', '--name-only', before, after).decode().strip(), PAGE)
        self.assertEqual(self.local_git('-C', remote, 'show', f'{after}:{PAGE}').decode(), markdown)
        stale = self.invoke('project_manager_wiki_update', dict(revision=before, markdown='stale'))
        self.assertEqual(stale['error']['code'], 'wiki_changed')
        self.assertEqual(len(pushes), 1)
