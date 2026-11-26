"""Exercise the bundled prompt's read-only approval check without GitHub."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from project_manager_engine.settings import SKILL, Settings


ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repository'
APP = 'fixture-app'
HEAD = 'a' * 40
OLD = 'b' * 40
WAITING = 'Awaiting human approval'
REVIEWER = {'login': 'reviewer', 'id': 2, 'type': 'User'}
AUTHOR = {'login': 'author', 'id': 1, 'type': 'User'}


def review(state='APPROVED', *, number=1, head=HEAD, user=REVIEWER):
    return dict(id=number, state=state, commit_id=head, user=user, submitted_at='2026-11-26T10:00:00Z')


class GitHubWorkflowPrompt(unittest.TestCase):
    pull = ('pr', 'view', '14', '--repo', REPO, '--json', 'author,headRefOid,isDraft,state')
    reviews = ('api', f'repos/{REPO}/pulls/14/reviews?per_page=100',
               '--method', 'GET', '--paginate', '--slurp')
    permission = ('api', f'repos/{REPO}/collaborators/reviewer/permission', '--method', 'GET')

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        (home / 'skills').mkdir()
        (home / 'skills' / SKILL).symlink_to(ROOT, target_is_directory=True)
        docs = (ROOT / 'doc' / 'PROFILE.md').read_text(encoding='utf-8')
        github = yaml.safe_load(re.search(r'```yaml\n(.*?)\n```', docs, re.DOTALL)[1])
        config = github['skills']['config'][SKILL]
        config['manager']['parameters'].update(REPO=REPO, github_identity=APP)
        for agent in (config['manager'], *config['workers']):
            agent['working_directory'] = temporary.name
        self.prompt = Settings.read(home, config).manager.prompt.render()[0]
        script = self.prompt.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        self.approval_check = compile(script, 'doc/MANAGER_PROMPT.md', 'exec')
        self.pr = dict(author=AUTHOR, headRefOid=HEAD, isDraft=False, state='OPEN',
                       mergeable='MERGEABLE', mergeStateStatus='CLEAN', reviewDecision='')
        self.responses = {
            self.pull: self.pr,
            self.reviews: [[]],
            self.permission: dict(permission='write', user=REVIEWER),
        }
        self.calls = []

    def github(self, command, **kwargs):
        self.assertEqual(command[:4], [APP, '--repo', REPO, 'gh'])
        request = tuple(command[4:])
        self.calls.append(request)
        self.assertIn(request, self.responses, f'Unexpected call, including a merge write: {request}')
        response = self.responses[request]
        if isinstance(response, Exception):
            raise response
        return json.dumps(response).encode()

    def check(self, github=None):
        self.calls.clear()
        stdout = StringIO()
        with patch.object(sys, 'argv', ['-', '14', HEAD, 'reviewer author']), redirect_stdout(stdout), patch(
                'subprocess.check_output', side_effect=github or self.github):
            exec(self.approval_check, {})
        return json.loads(stdout.getvalue())

    def test_rendered_workflow_requires_fresh_approval_before_direct_sha_pinned_merge(self):
        procedure = self.prompt.split('## Merging conditions\n', 1)[1].split('### Human approval check\n', 1)[0]
        write = procedure.index(f'{APP} --repo {REPO} gh api --method PUT')
        conditions = procedure[:write]
        for required in ('clean worker verdict', 'no outstanding actionable feedback', 'GitHub rules and required checks',
                         'Immediately before merging', '**Human approval verified** for the exact reviewed SHA',
                         'even if GitHub requires zero approvals', '**Awaiting human approval** under **Need attention**',
                         'Leave auto-merge and queue admission to the operator'):
            self.assertIn(required, conditions)
        self.assertIn(f'"repos/{REPO}/pulls/${{pr_number}}/merge"', procedure[write:])
        self.assertIn('--raw-field "sha=${reviewed_head}"', procedure[write:])

    def test_unprotected_pr_with_green_checks_and_zero_reviews_waits(self):
        self.assertEqual(self.check(), {'status': WAITING})
        self.assertEqual(self.calls, [self.pull, self.reviews])

    def test_invalid_review_evidence_waits(self):
        for evidence in (
            [[review(head=OLD)]],
            [[review(user=AUTHOR)]],
            [[review(user=dict(REVIEWER, type='Bot'))]],
            [[review('PENDING')]],
            [[review()], [review('DISMISSED', number=2)]],
            [[review()], [review('DISMISSED', number=2, user={'id': REVIEWER['id']})]],
            [[dict(review(), submitted_at=None)]],
            [[review()], {}],
            subprocess.CalledProcessError(1, self.reviews),
        ):
            with self.subTest(evidence=evidence):
                self.responses[self.reviews] = evidence
                self.assertEqual(self.check(), {'status': WAITING})
                self.assertEqual(self.calls, [self.pull, self.reviews])

    def test_reviewer_without_current_write_access_waits(self):
        self.responses[self.reviews] = [[review()]]
        self.responses[self.permission] = dict(permission='read', user=REVIEWER)
        self.assertEqual(self.check(), {'status': WAITING})
        self.assertEqual(self.calls, [self.pull, self.reviews, self.permission])

    def test_qualifying_approval_is_verified_after_permission_and_review_rereads(self):
        self.responses[self.reviews] = [[review(head=OLD)], [review(number=2), review('COMMENTED', number=3)]]
        self.assertEqual(self.check(), {'status': 'Human approval verified', 'head': HEAD, 'reviewer': 'reviewer'})
        self.assertEqual(self.calls, [self.pull, self.reviews, self.permission, self.pull, self.reviews])

    def test_dismissal_during_eligibility_check_waits_even_with_same_head(self):
        self.responses[self.reviews] = [[review()]]

        def dismiss(command, **kwargs):
            response = self.github(command, **kwargs)
            if tuple(command[4:]) == self.permission:
                self.responses[self.reviews] = [[review('DISMISSED')]]
            return response

        self.assertEqual(self.check(dismiss), {'status': WAITING})
        self.assertEqual(self.calls, [self.pull, self.reviews, self.permission, self.pull, self.reviews])

    def test_head_change_during_eligibility_check_requires_worker_rereview(self):
        self.responses[self.reviews] = [[review()]]

        def advance(command, **kwargs):
            response = self.github(command, **kwargs)
            if tuple(command[4:]) == self.permission:
                self.pr['headRefOid'] = OLD
            return response

        self.assertEqual(self.check(advance), {'status': 'Head changed', 'head': OLD})
        self.assertEqual(self.calls, [self.pull, self.reviews, self.permission, self.pull])
