"""Run the rendered GitHub workflow merge command with isolated API fixtures."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
from string import Template
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from project_manager_engine.settings import SKILL, Settings
from workflows.github.github_app import GitHubError, main


ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repository'
APP = 'fixture-app'
HEAD = 'a' * 40
OLD = 'b' * 40
MERGE_SHA = 'c' * 40
WAITS = 'Awaiting human approval'
PR = '/repos/owner/repository/pulls/14'
PERMISSION = '/repos/owner/repository/collaborators/reviewer/permission'
RULES = '/repos/owner/repository/rules/branches/main?per_page=100&page=1'
REVIEWS = PR + '/reviews?per_page=100&page=1'
NEXT_REVIEWS = PR + '/reviews?per_page=100&page=2'
REVIEWER = {'login': 'reviewer', 'id': 2, 'type': 'User'}
AUTHOR = {'login': 'author', 'id': 1, 'type': 'User'}


def review(state='APPROVED', *, number=1, head=HEAD, user=REVIEWER):
    return dict(id=number, state=state, commit_id=head, user=user, submitted_at='2026-11-26T10:00:00Z')


class GitHubWorkflowPrompt(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        (home / 'skills').mkdir()
        (home / 'skills' / SKILL).symlink_to(ROOT, target_is_directory=True)
        guide = (ROOT / 'workflows' / 'github' / 'README.md').read_text(encoding='utf-8')
        github = yaml.safe_load(re.search(r'```yaml\n(.*?)\n```', guide, re.DOTALL)[1])
        config = github['skills']['config'][SKILL]
        config['manager']['parameters'].update(REPO=REPO, github_identity=APP)
        for agent in (config['manager'], *config['workers']):
            agent['working_directory'] = temporary.name
        prompt = Settings.read(home, config).manager.prompt.render(setup={'setup_launcher': APP})[0]
        procedure = prompt.split('## Merging conditions\n', 1)[1].split('## Worker result\n', 1)[0]
        command = re.search(r'```bash\n(.*?)\n\s*```', procedure, re.DOTALL)[1].replace('\\\n', '')
        self.command = Template(command)
        self.procedure = procedure
        config_path = home / 'herdr-project-manager' / 'github' / f'{APP}.json'
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(dict(app_slug=APP, app_id=77, repository=REPO,
                                               private_key=str(home / 'app.pem'))))
        config_path.chmod(0o600)
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pull = dict(author=AUTHOR, headRefOid=HEAD, isDraft=False, state='OPEN', baseRefName='main',
                         autoMergeRequest=None, isInMergeQueue=False, isMergeQueueEnabled=False,
                         mergeable='MERGEABLE',
                         mergeStateStatus='CLEAN', reviewDecision=None)
        self.settings = dict(nameWithOwner=REPO, mergeCommitAllowed=True, squashMergeAllowed=True,
                             rebaseMergeAllowed=False, pullRequest=self.pull)
        self.first = [review()]
        self.final = None
        self.incomplete_read = None
        self.rules = []
        self.permission = dict(permission='write', user=REVIEWER)
        self.on_permission = lambda: None
        self.merge_response = dict(merged=True, sha=MERGE_SHA)
        self.calls = []
        self.review_reads = 0

    def request(self, method, path, token, data=None):
        self.calls.append((method, path, data))
        bootstrap = {
            '/app': {'id': 77, 'slug': APP},
            f'/repos/{REPO}/installation': {'id': 42, 'app_id': 77, 'account': {'login': 'owner'}},
            '/app/installations/42/access_tokens': {
                'token': 'installation-fixture-token', 'expires_at': '2026-11-26T11:00:00Z',
                'permissions': {'contents': 'write'},
            },
            f'/repos/{REPO}': {'full_name': REPO},
            f'/users/{APP}%5Bbot%5D': {'id': 70, 'login': f'{APP}[bot]', 'type': 'Bot'},
        }
        if path in bootstrap:
            return bootstrap[path], ''
        self.assertEqual(token, 'installation-fixture-token')
        if path == '/graphql' and method == 'POST':
            self.assertEqual(data['variables'], {'owner': 'owner', 'name': 'repository', 'number': 14})
            self.assertIn('isInMergeQueue isMergeQueueEnabled', data['query'])
            return {'data': {'repository': deepcopy(self.settings)}}, ''
        if path == RULES and method == 'GET':
            return self.rules, ''
        if path == REVIEWS and method == 'GET':
            self.review_reads += 1
            evidence = self.first if self.review_reads == 1 or self.final is None else self.final
            if isinstance(evidence, Exception):
                raise evidence
            links = f'<https://api.github.com{NEXT_REVIEWS}>; rel="next"' if (
                self.review_reads == self.incomplete_read) else ''
            return evidence, links
        if path == NEXT_REVIEWS and method == 'GET':
            return [], ''
        if path == PERMISSION and method == 'GET':
            self.on_permission()
            return self.permission, ''
        if path == PR + '/merge' and method == 'PUT':
            if isinstance(self.merge_response, Exception):
                raise self.merge_response
            return self.merge_response, ''
        self.fail(f'Unexpected GitHub call {method} {path}')

    def run_command(self, *, worker='clean', clear=True, head=HEAD, humans='reviewer author', method='squash'):
        template = self.command.substitute(pr_number='14', reviewed_head=head, verified_human_logins=humans,
                                           merge_method=method)
        launcher, *args = shlex.split(template)
        self.assertEqual(launcher, APP)
        if worker is None:
            index = args.index('--worker-verdict')
            del args[index:index + 2]
        else:
            args[args.index('--worker-verdict') + 1] = worker
        if not clear:
            args.remove('--feedback-clear')
        self.review_reads = 0
        self.calls.clear()
        stdout = StringIO()
        with (patch.object(sys, 'argv', [APP]), redirect_stdout(stdout),
              patch('workflows.github.github_app.jwt', return_value='app-proof'),
              patch('workflows.github.github_app.request', side_effect=self.request)):
            self.assertEqual(main(args), 0)
        return json.loads(stdout.getvalue())

    def assert_hold(self, status='Need attention', **options):
        self.assertEqual(self.run_command(**options)['status'], status)
        self.assertFalse(any(method in {'PUT', 'PATCH', 'DELETE'} for method, _, _ in self.calls))

    def test_rendered_workflow_and_qualifying_write(self):
        for required in ('clean verdict', 'no outstanding actionable feedback', 'required checks',
                         'even if GitHub requires zero approvals', 'after checking collaborator access',
                         'Leave auto-merge and queue admission to the operator'):
            self.assertIn(required, self.procedure)
        self.assertEqual(self.run_command(), dict(status='Merged', head=HEAD, merge_sha=MERGE_SHA))
        self.assertEqual([(method, path) for method, path, _ in self.calls[-8:]], [
            ('POST', '/graphql'), ('GET', RULES), ('GET', REVIEWS), ('GET', PERMISSION),
            ('POST', '/graphql'), ('GET', RULES), ('GET', REVIEWS), ('PUT', PR + '/merge'),
        ])
        self.assertEqual(self.calls[-1][2], {'sha': HEAD, 'merge_method': 'squash'})
        self.assertEqual(self.calls[2], ('POST', '/app/installations/42/access_tokens', {'repositories': ['repository']}))

    def test_holds_without_confirmed_worker_evidence_or_clear_feedback(self):
        for options in ({'worker': 'changes_requested'}, {'worker': None}, {'clear': False}, {'head': HEAD[:7]}):
            with self.subTest(options=options):
                self.assert_hold(**options)
                self.assertNotIn('/graphql', [path for _, path, _ in self.calls])

    def test_holds_for_each_unready_github_state(self):
        cases = [
            {'state': 'CLOSED'}, {'isDraft': True}, {'mergeable': 'CONFLICTING'},
            {'mergeStateStatus': 'UNSTABLE'}, {'mergeStateStatus': 'BLOCKED'},
            {'reviewDecision': 'CHANGES_REQUESTED'}, {'autoMergeRequest': {'enabledAt': 'today'}},
            {'isInMergeQueue': True}, {'isMergeQueueEnabled': True},
        ]
        for changes in cases:
            with self.subTest(changes=changes), patch.dict(self.pull, changes):
                self.assert_hold()

    def test_holds_for_a_github_review_requirement(self):
        self.pull.update(reviewDecision='REVIEW_REQUIRED', mergeStateStatus='BLOCKED')
        self.assert_hold(WAITS)

    def test_holds_for_queue_rule_or_disallowed_merge_method(self):
        self.assert_hold(method='rebase')
        for rule, method in (
            ({'type': 'merge_queue'}, 'squash'),
            ({'type': 'required_linear_history'}, 'merge'),
            ({'type': 'pull_request', 'parameters': {'allowed_merge_methods': ['rebase']}}, 'squash'),
        ):
            with self.subTest(rule=rule):
                self.rules = [rule]
                self.assert_hold(method=method)

    def test_missing_or_invalid_approval_never_writes(self):
        cases = [
            [], [review(head=OLD)], [review(user=AUTHOR)], [review('PENDING')],
            [review(user={'login': 'reviewer', 'id': 2, 'type': 'Bot'})],
            [review(), review('DISMISSED', number=2)],
            [dict(review(), submitted_at=None)], [review(), review()],
            GitHubError('Unavailable'),
        ]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.first = evidence
                self.assert_hold(WAITS)
        self.assert_hold(WAITS, humans='')

    def test_unresolved_changes_request_blocks_even_on_unprotected_branch(self):
        self.first.append(review('CHANGES_REQUESTED', number=2, user={
            'login': 'other-reviewer', 'id': 3, 'type': 'User',
        }))
        self.assert_hold()

    def test_reviewer_without_verified_current_write_access_waits(self):
        for permission in (
            dict(permission='read', user=REVIEWER),
            dict(permission='write', user=dict(REVIEWER, id=999)),
        ):
            with self.subTest(permission=permission):
                self.permission = permission
                self.assert_hold(WAITS)

    def test_dismissal_after_eligibility_check_waits(self):
        self.final = [review('DISMISSED')]
        self.assert_hold(WAITS)
        self.assertEqual(self.review_reads, 2)

    def test_incomplete_dismissal_waits_on_both_review_reads(self):
        incomplete = [
            {'id': 2, 'state': 'DISMISSED', 'user': {'id': 3, 'login': 'other-reviewer'}},
            dict(review('DISMISSED', number=2, user={'id': 0, 'login': ' ', 'type': 'User'})),
            dict(review('DISMISSED', number=2, user=AUTHOR), commit_id=None, submitted_at=None),
        ]
        for dismissal in incomplete:
            with self.subTest(dismissal=dismissal):
                self.first, self.final = [review(), dismissal], None
                self.assert_hold(WAITS)
                self.first, self.final = [review()], [review(), dismissal]
                self.assert_hold(WAITS)
                self.assertEqual(self.review_reads, 2)

    def test_empty_continuation_blocks_initial_review_history(self):
        self.incomplete_read = 1
        self.assert_hold(WAITS)
        self.assertEqual(self.review_reads, 1)
        self.assertEqual(self.calls[-1][:2], ('GET', NEXT_REVIEWS))

    def test_empty_continuation_blocks_final_review_history(self):
        self.incomplete_read = 2
        self.assert_hold(WAITS)
        self.assertEqual(self.review_reads, 2)
        self.assertEqual(self.calls[-1][:2], ('GET', NEXT_REVIEWS))

    def test_changed_head_requires_a_new_worker_review(self):
        self.assert_hold('Head changed', head=OLD)
        self.on_permission = lambda: self.pull.update(headRefOid=OLD)
        self.assertEqual(self.run_command(), {'status': 'Head changed', 'head': OLD})
        self.assertEqual(self.review_reads, 1)
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.calls))

    def test_uncertain_merge_is_not_retried(self):
        self.merge_response = GitHubError('Unconfirmed write')
        result = self.run_command()
        self.assertEqual(result['status'], 'Need attention')
        self.assertIn('Reconcile GitHub state', result['reason'])
        self.assertEqual(sum(method == 'PUT' for method, _, _ in self.calls), 1)
