import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from project_manager_engine.interface import outcome, parse
from project_manager_engine.settings import SKILL


ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repository'
APP = 'example-app'
HEAD = 'a' * 40
OLD_HEAD = 'b' * 40
AWAITING_APPROVAL = 'Awaiting human approval'
HUMAN = {'login': 'reviewer', 'id': 2, 'type': 'User'}
AUTHOR = {'login': 'author', 'id': 1, 'type': 'User'}


def review(number, *, user=HUMAN, state='APPROVED', head=HEAD):
    return dict(id=number, user=user, state=state, commit_id=head, submitted_at='2026-11-26T10:00:00Z')


class GitHubHandoff(unittest.TestCase):
    status = ('status',)
    pull = ('gh', 'pr', 'view', '14', '--repo', REPO, '--json',
            'author,headRefOid,isDraft,mergeable,mergeStateStatus,reviewDecision,state')
    reviews = ('gh', 'api', f'repos/{REPO}/pulls/14/reviews?per_page=100',
               '--method', 'GET', '--paginate', '--slurp')
    permission = ('gh', 'api', f'repos/{REPO}/collaborators/reviewer/permission', '--method', 'GET')

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        (self.home / 'skills').mkdir()
        (self.home / 'skills' / SKILL).symlink_to(ROOT, target_is_directory=True)
        docs = (ROOT / 'doc' / 'PROFILE.md').read_text(encoding='utf-8')
        self.config = yaml.safe_load(re.search(r'```yaml\n(.*?)\n```', docs, re.DOTALL)[1])
        raw = self.config['skills']['config'][SKILL]
        self.parameters = raw['manager']['parameters']
        self.parameters.update(REPO=REPO, github_identity=APP, human_reviewers='reviewer, author')
        for agent in (raw['manager'], *raw['workers']):
            agent['working_directory'] = str(self.home)
        self.pr = {'author': {'login': 'author', 'is_bot': False}, 'headRefOid': HEAD,
                   'isDraft': False, 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN',
                   'reviewDecision': '', 'state': 'OPEN'}
        self.replies = {
            self.status: dict(authenticated=True, actor=f'{APP}[bot]', repository=REPO),
            self.pull: self.pr,
            self.reviews: [[]],
            self.permission: dict(permission='write', user=HUMAN),
        }
        mock = patch('project_manager_engine.github_handoff.subprocess.run', side_effect=self.respond)
        self.run = mock.start()
        self.addCleanup(mock.stop)

    def respond(self, command, **kwargs):
        self.assertEqual(command[:3], [APP, '--repo', REPO])
        invocation = tuple(command[3:])
        self.assertIn(invocation, self.replies, f'Unexpected command, including any merge write: {invocation}')
        reply = self.replies[invocation]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return SimpleNamespace(stdout=reply)
        return SimpleNamespace(stdout=json.dumps(reply))

    def handoff(self):
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config), encoding='utf-8')
        command, args = parse(f'github-handoff 14 {HEAD}')
        response = outcome(self.home, command, args)
        self.assertTrue(response['ok'], response)
        return response['result']

    def expect(self, status, invocations):
        result = self.handoff()
        self.assertEqual((result['section'], result['status']), ('Need attention', status))
        self.assertEqual(result['reviewed_head'], HEAD)
        self.assertEqual([tuple(call.args[0][3:]) for call in self.run.call_args_list], invocations)
        return result

    def test_clean_passing_unprotected_pr_without_reviews_never_writes(self):
        # The handoff receives the clean worker's SHA. GitHub reports no required review and clean checks.
        self.expect(AWAITING_APPROVAL, [self.status, self.pull, self.reviews])

    def test_bot_author_outsider_and_unsubmitted_reviews_do_not_count(self):
        bot = dict(HUMAN, type='Bot')
        outsider = dict(HUMAN, login='outsider')
        self.replies[self.reviews] = [[review(1, user=bot), review(2, user=AUTHOR), review(3, user=outsider),
                                      review(4, state='COMMENTED'), review(5, state='PENDING')]]
        self.expect(AWAITING_APPROVAL, [self.status, self.pull, self.reviews])

    def test_stale_dismissed_and_superseded_reviews_do_not_count(self):
        for newer in (review(2, head=OLD_HEAD), review(2, state='DISMISSED'),
                      review(2, state='CHANGES_REQUESTED')):
            with self.subTest(newer=newer):
                self.run.reset_mock()
                self.replies[self.reviews] = [[review(1)], [newer]]
                self.expect(AWAITING_APPROVAL, [self.status, self.pull, self.reviews])

    def test_review_lookup_failure_and_unverifiable_evidence_keep_approval_pending(self):
        for failure in (subprocess.CalledProcessError(1, self.reviews), 'invalid json',
                        [[review(1)], {}], [[review(1)], [review(1, state='DISMISSED')]],
                        [[dict(review(1), submitted_at=None)]]):
            with self.subTest(failure=failure):
                self.run.reset_mock()
                self.replies[self.reviews] = failure
                self.expect(AWAITING_APPROVAL, [self.status, self.pull, self.reviews])

    def test_review_requires_current_collaborator_write_access_and_identity(self):
        self.replies[self.reviews] = [[review(1)]]
        for permission in (dict(permission='read', user=HUMAN), dict(permission='write', user=dict(HUMAN, id=3)),
                           subprocess.CalledProcessError(1, self.permission)):
            with self.subTest(permission=permission):
                self.run.reset_mock()
                self.replies[self.permission] = permission
                self.expect(AWAITING_APPROVAL, [self.status, self.pull, self.reviews, self.permission])

    def test_exact_head_human_approval_can_only_handoff_to_human(self):
        self.replies[self.reviews] = [[review(1, head=OLD_HEAD)], [review(2), review(3, state='COMMENTED')]]
        result = self.expect('Awaiting human merge',
                             [self.status, self.pull, self.reviews, self.permission, self.pull])
        self.assertEqual((result['head'], result['approver']), (HEAD, 'reviewer'))

    def test_approved_pr_still_needs_github_rules_and_checks(self):
        self.replies[self.reviews] = [[review(1)]]
        for merge_status, decision in [('CLEAN', 'REVIEW_REQUIRED'), ('BLOCKED', 'APPROVED'),
                                       ('UNKNOWN', '')]:
            with self.subTest(merge_status=merge_status, decision=decision):
                self.run.reset_mock()
                self.pr.update(mergeStateStatus=merge_status, reviewDecision=decision)
                self.expect('Awaiting GitHub requirements',
                            [self.status, self.pull, self.reviews, self.permission, self.pull])

    def test_head_change_during_approval_lookup_invalidates_handoff(self):
        self.replies[self.reviews] = [[review(1)]]

        def changed_head(command, **kwargs):
            result = self.respond(command, **kwargs)
            if tuple(command[3:]) == self.permission:
                self.pr['headRefOid'] = OLD_HEAD
            return result

        self.run.side_effect = changed_head
        result = self.expect('Head changed', [self.status, self.pull, self.reviews, self.permission, self.pull])
        self.assertEqual(result['head'], OLD_HEAD)
        self.assertNotIn('approver', result)

    def test_no_operator_verified_accounts_keeps_approval_pending(self):
        del self.parameters['human_reviewers']
        self.replies[self.reviews] = [[review(1)]]
        self.expect(AWAITING_APPROVAL, [self.status, self.pull])

    def test_unverified_app_stops_before_github_reads(self):
        self.replies[self.status]['actor'] = 'different-app[bot]'
        self.expect(AWAITING_APPROVAL, [self.status])
