"""Exercise Hermes's real tool dispatcher without a gateway or GitHub credentials."""

import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from project_manager_engine.hermes_native import Native
from project_manager_engine.manager_policy import BLOCK, SAFE_TOOLS, TOOLSET
from project_manager_engine.settings import SKILL


ROOT = Path(__file__).resolve().parents[1]
APP = 'fixture-app'
REPO = 'fixture/repository'
SID = 'disposable-manager'


@unittest.skipUnless(importlib.util.find_spec('model_tools'), 'Requires the Hermes agent runtime')
class NativeHermesDispatch(unittest.TestCase):
    def test_direct_and_deferred_dispatch_deny_repository_writes(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'HERMES_HOME': directory}):
            home = Path(directory)
            (home / 'plugins').mkdir()
            (home / 'plugins' / SKILL).symlink_to(ROOT, target_is_directory=True)
            manager = dict(name='manager', model='fixture', description='Fixture manager',
                           working_directory=str(home), prompt_file=str(ROOT / 'doc' / 'MANAGER_PROMPT.md'),
                           parameters=dict(REPO=REPO, LABEL='opt-in', IMPLEMENTER='implementer',
                                           REVIEWER='reviewer', github_identity=APP))
            raw = dict(enabled=True, manager=manager, max_working_panes=1, workers=[])
            config = dict(plugins={'enabled': [SKILL]}, skills={'config': {SKILL: raw}})
            (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
            native = Native(home)
            native.save_binding(dict(workspace='w1', session_id=SID, github_scoped=True))

            # The executable exists only in this profile. An unexpected invocation is recorded and fails.
            calls = home / 'app-calls'
            executable = home / APP
            executable.write_text(
                f'#!{sys.executable}\n'
                'import json, sys\n'
                'from pathlib import Path\n'
                f'with Path({str(calls)!r}).open("a") as stream: print(json.dumps(sys.argv[1:]), file=stream)\n'
                f'if sys.argv[1:] == ["--repo", "{REPO}", "status"]:\n'
                f'    print(json.dumps(dict(authenticated=True, actor="{APP}[bot]", repository="{REPO}")))\n'
                f'elif sys.argv[1:8] == ["--repo", "{REPO}", "gh", "issue", "list", "--repo", "{REPO}"]:\n'
                '    print(json.dumps([{"number": 14}]))\n'
                'else:\n'
                '    raise SystemExit(2)\n', encoding='utf-8')
            executable.chmod(0o700)
            os.environ['PATH'] = str(home) + os.pathsep + os.environ['PATH']

            from gateway.session_policy import _resolve_toolsets
            from hermes_cli.plugins import discover_plugins
            from model_tools import get_tool_definitions, handle_function_call
            from tools.registry import registry

            discover_plugins(force=True)
            scoped = sorted(_resolve_toolsets({'toolsets': [TOOLSET]}, {}, 'tui', False))

            def names(toolsets):
                return {definition['function']['name'] for definition in get_tool_definitions(
                    enabled_toolsets=toolsets, quiet_mode=True, skip_tool_search_assembly=True)}

            self.assertEqual(names(scoped), SAFE_TOOLS)
            write = 'fixture_github_write'
            forbidden = Mock(side_effect=AssertionError('Repository write executed'))
            registry.register(
                name=write, toolset='fixture_github', handler=forbidden, scope=str(home),
                schema=dict(name=write, description='Fixture write', parameters={
                    'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command'],
                }))
            broad = [*scoped, 'terminal', 'fixture_github']
            # Exercise the execution guard with an inventory that would be refused at admission.
            self.assertIn(write, names(broad))

            def dispatch(name, args, toolsets):
                return json.loads(handle_function_call(name, args, session_id=SID, enabled_toolsets=toolsets))

            def deferred(name, args, toolsets):
                return dispatch('tool_call', {'calls': [{'name': name, 'arguments': args}]}, toolsets)

            for binding in (dict(workspace='w1', session_id=SID, github_scoped=True),
                            dict(workspace='w1', session_id=SID)):
                native.save_binding(binding)
                for command in ('gh pr merge 14', 'gh pr merge 14 --auto', 'gh api graphql enqueuePullRequest',
                                'git push origin HEAD:refs/heads/main'):
                    with self.subTest(scoped=binding.get('github_scoped'), command=command):
                        args = {'command': f'{APP} --repo {REPO} {command}'}
                        self.assertIn(BLOCK['message'], dispatch('terminal', args, broad).get('error', ''))
                        self.assertIn('error', deferred('terminal', args, scoped))
                        self.assertIn(BLOCK['message'], deferred(write, args, broad).get('error', ''))
            forbidden.assert_not_called()
            self.assertFalse(calls.exists())

            native.save_binding(dict(workspace='w1', session_id=SID, github_scoped=True))
            self.assertEqual(deferred('project_manager_github_issues', {}, scoped),
                             {'ok': True, 'result': {'issues': [{'number': 14}]}})
            self.assertEqual(len(calls.read_text(encoding='utf-8').splitlines()), 2)
