"""Check the rendered GitHub workflow with isolated fixtures."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import re
import shlex
from string import Template
from subprocess import CompletedProcess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from project_manager_engine.settings import SKILL, Prompt, Settings
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
DISPATCH_SECTIONS = ('Periodic turn', 'Shared scheduling', 'Worker queues and admission', 'Worker result')


def review(state='APPROVED', *, number=1, head=HEAD, user=REVIEWER):
    return dict(id=number, state=state, commit_id=head, user=user, submitted_at='2026-11-26T10:00:00Z')


def prompt_section(prompt, heading):
    section, = re.findall(rf'^## {re.escape(heading)}\n(.*?)(?=^## |\Z)', prompt, re.MULTILINE | re.DOTALL)
    return section.strip()


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
        self.workflows = Settings.read(home, config)
        prompt = self.workflows.manager.prompt.render()[0]
        procedure = prompt.split('## Merging conditions\n', 1)[1].split('## Worker result\n', 1)[0]
        command = re.search(r'```bash\n(.*?)\n\s*```', procedure, re.DOTALL)[1].replace('\\\n', '')
        self.command = Template(command)
        self.procedure = procedure
        self.config_path = home / f'{APP}.json'
        self.config_path.write_text(json.dumps(dict(app_slug=APP, app_id=77, repository=REPO,
                                                   private_key=str(home / 'app.pem'))))
        self.config_path.chmod(0o600)
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

    def admission_policy(self, prompt, *observations):
        section = prompt_section(prompt, 'Worker queues and admission')
        rows = [line.strip('| ').split(' | ') for line in section.splitlines() if line.startswith('| ')]
        columns, _, *rules = rows
        for rule in rules:
            decision = dict(zip(columns, rule, strict=True))
            if decision['Observation'] in observations:
                return decision
        self.fail(f'No rendered admission rule matched {observations!r}')

    def assert_dispatch_prose(self, prompt):
        # The model sees the prose as well as the table. Guard the full dispatch sections
        # so an additional instruction cannot silently contradict a checked table row.
        fixture = Prompt.read(ROOT, 'workflow dispatch fixture', 'tests/fixtures/github_dispatch_prose.md',
                              self.workflows.manager.prompt.defaults, manager=True).render()[0]
        for heading in DISPATCH_SECTIONS:
            lines = prompt_section(prompt, heading).splitlines()
            prose = '\n'.join(line for line in lines if not line.startswith('| '))
            prose = re.sub(r'\n{3,}', '\n\n', prose).strip()
            self.assertMultiLineEqual(prompt_section(fixture, heading), prose, f'{heading} dispatch prose changed')

    def test_dispatch_prose_matches_the_workflow_contract(self):
        self.assert_dispatch_prose(self.workflows.manager.prompt.render()[0])

    def test_dispatch_prose_rejects_conflicting_instructions_with_the_table_intact(self):
        prompt = self.workflows.manager.prompt.render()[0]
        mutations = (
            'On a `capacity` refusal, move the issue to **Need attention** as a blocker.',
            'On an `outcome_unknown` receipt, move the pending feedback to assigned.',
            'On an `outcome_unknown` receipt, redispatch as soon as a slot is free, before reconciliation.',
        )
        observations = ('Capacity wait', 'Unresolved dispatch')
        for heading in DISPATCH_SECTIONS:
            for instruction in mutations:
                with self.subTest(heading=heading, instruction=instruction):
                    boundary = f'## {heading}\n\n'
                    if heading == 'Worker queues and admission':
                        boundary = '| Keep pending | Record intent and dispatch once |\n'
                    mutated = prompt.replace(boundary, f'{boundary}{instruction}\n\n', 1)
                    self.assertNotEqual(mutated, prompt)
                    for observation in observations:
                        self.assertEqual(self.admission_policy(mutated, observation),
                                         self.admission_policy(prompt, observation))
                    with self.assertRaisesRegex(AssertionError, f'{heading} dispatch prose changed'):
                        self.assert_dispatch_prose(mutated)

    def one_slot_trace(self, prompt, event, queued_hold):
        def steps(heading):
            return re.findall(r'^\d+\. \*\*([^\n*]+)\.\*\*', prompt_section(prompt, heading), re.MULTILINE)

        slots = 1
        trace = []
        candidates = {'New issues': []}
        result = dict(issue='A', pane_id='w1:p2', turn_id='completed-A')
        placements = {'A': 'Review placement', 'B': 'Implementation placement'}
        for step in steps(event):
            if step == 'Read both queues':
                candidates['Previously queued'] = ['B']
            elif step in ('Refresh tracked work', 'Record the result'):
                candidates['Other tracked work'] = ['A']
                trace.append(('record pending', result['issue']))
            elif step == 'Acknowledge the result':
                trace.append(('mark-read', result['pane_id'], result['turn_id']))
            elif step == 'Schedule work':
                for priority in steps('Shared scheduling'):
                    for issue in candidates[priority]:
                        observations = ['Ready to dispatch' if slots else 'Capacity wait']
                        if issue == 'B' and queued_hold:
                            observations.append(queued_hold)
                        decision = self.admission_policy(prompt, *observations)
                        if decision['Next action'] == 'Record intent and dispatch once':
                            trace.append(('dispatch', issue))
                            slots -= 1
                        else:
                            trace.append((decision['Next action'], issue, decision[placements[issue]]))
            else:
                self.fail(f'Unexpected rendered step in {event}: {step}')
        return trace

    def test_both_paths_give_eligible_queued_work_the_one_slot_before_a_new_follow_up(self):
        prompt = self.workflows.manager.prompt.render()[0]
        for event, acknowledgement in (('Periodic turn', []),
                                       ('Worker result', [('mark-read', 'w1:p2', 'completed-A')])):
            with self.subTest(event=event):
                self.assertEqual(self.one_slot_trace(prompt, event, queued_hold=None), [
                    ('record pending', 'A'), *acknowledgement,
                    ('dispatch', 'B'), ('Wait for capacity', 'A', 'In review → Queue'),
                ])
                for hold, action in (('Genuine blocker', 'Wait for resolution'),
                                     ('Unresolved dispatch', 'Reconcile without dispatching')):
                    with self.subTest(hold=hold):
                        self.assertEqual(self.one_slot_trace(prompt, event, queued_hold=hold), [
                            ('record pending', 'A'), *acknowledgement,
                            (action, 'B', 'Need attention'), ('dispatch', 'A'),
                        ])

    def assert_capacity_queues(self, prompt):
        decision = self.admission_policy(prompt, 'Capacity wait')
        self.assertEqual(decision['Implementation placement'], 'In progress → Queue')
        self.assertEqual(decision['Review placement'], 'In review → Queue')
        self.assertEqual(decision['Feedback'], 'Keep pending')
        self.assertEqual(decision['Next action'], 'Wait for capacity')

    def test_configured_reviewer_routes_all_review_admissions(self):
        prompt = self.workflows.manager.prompt.render({'REVIEWER': 'review-prod'})[0]
        self.assertIn('Route new PR reviews, queued reviews, and reviews of changed heads to `review-prod`.', prompt)
        self.assertIn('Pass that exact name to `run`, or `steer` an eligible pane only after verifying '
                      'that it belongs to that worker and issue.', prompt)

    def test_full_capacity_then_verified_admission(self):
        prompt = self.workflows.manager.prompt.render()[0]
        self.assertIn("Use the first matching row to decide the issue entry's placement, feedback update, and next action.",
                      prompt)
        self.assertIn('zero available slots or a `capacity` refusal from `run` or `steer`', prompt)
        self.assert_capacity_queues(prompt)

        available = self.admission_policy(prompt, 'Ready to dispatch')
        self.assertEqual(available['Implementation placement'], 'Keep current placement')
        self.assertEqual(available['Review placement'], 'Keep current placement')
        self.assertEqual(available['Feedback'], 'Keep pending')
        self.assertEqual(available['Next action'], 'Record intent and dispatch once')

        admitted = self.admission_policy(prompt, 'Verified admission')
        self.assertEqual(admitted['Implementation placement'], 'In progress')
        self.assertEqual(admitted['Review placement'], 'In review')
        self.assertEqual(admitted['Feedback'], 'Assign only the admitted batch')
        self.assertEqual(admitted['Next action'], 'Follow the admitted turn')
        self.assertIn('Move that entry between headings and subsections in a single dashboard update', prompt)
        self.assertIn('After each outcome, update the same entry using the table.', prompt)

    def test_capacity_regression_rejects_need_attention_mutation(self):
        prompt = self.workflows.manager.prompt.render()[0]
        for queue in ('In progress → Queue', 'In review → Queue'):
            with self.subTest(queue=queue):
                mutated = re.sub(r'^\| Capacity wait \|[^\n]+',
                                 lambda row: row[0].replace(queue, 'Need attention'), prompt, flags=re.MULTILINE)
                self.assertNotEqual(mutated, prompt)
                with self.assertRaisesRegex(AssertionError, 'Need attention'):
                    self.assert_capacity_queues(mutated)

    def test_uncertain_receipts_keep_pending_feedback_and_prevent_redispatch(self):
        prompt = self.workflows.manager.prompt.render()[0]
        self.assertIn('a missing or mismatched receipt, `outcome_unknown`, or `admission: unknown`', prompt)
        self.assertIn('A free slot, a missing pane, or the previous turn in the target pane leaves the dispatch unresolved.',
                      prompt)
        for observation in ('Capacity wait', 'Ready to dispatch'):
            with self.subTest(observation=observation):
                held = self.admission_policy(prompt, observation, 'Unresolved dispatch')
                self.assertEqual(held['Implementation placement'], 'Need attention')
                self.assertEqual(held['Review placement'], 'Need attention')
                self.assertEqual(held['Feedback'], 'Keep pending')
                self.assertEqual(held['Next action'], 'Reconcile without dispatching')

        recovered = self.admission_policy(prompt, 'Verified admission', 'Ready to dispatch')
        self.assertEqual(recovered['Feedback'], 'Assign only the admitted batch')
        self.assertEqual(recovered['Next action'], 'Follow the admitted turn')
        self.assertIn('Retry only after verifying that submission was never attempted', prompt)

    def test_operator_and_merge_blockers_take_precedence_over_capacity(self):
        prompt = self.workflows.manager.prompt.render()[0]
        self.assertIn('PRs queued or scheduled for merge in GitHub', prompt)
        self.assertIn('List **Need attention** first, followed by **In progress**, **In review**, and **Done**.', prompt)
        decision = self.admission_policy(prompt, 'Capacity wait', 'Genuine blocker')
        self.assertEqual(decision['Implementation placement'], 'Need attention')
        self.assertEqual(decision['Review placement'], 'Need attention')
        self.assertEqual(decision['Next action'], 'Wait for resolution')

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
            self.assertEqual(main(args, config_path=self.config_path), 0)
        return json.loads(stdout.getvalue())

    def assert_hold(self, status='Need attention', **options):
        self.assertEqual(self.run_command(**options)['status'], status)
        self.assertFalse(any(method in {'PUT', 'PATCH', 'DELETE'} for method, _, _ in self.calls))

    def test_reviewer_publication_is_an_exact_head_comment(self):
        prompt, _ = self.workflows.workers['reviewer'].prompt.render(
            dict(REPO=REPO, ISSUE='12', PR='14', HEAD=HEAD, github_identity=APP))
        command = re.search(r'```bash\n(.*?)\n```', prompt, re.DOTALL)[1].replace('\\\n', '')
        for verdict in ('clean', 'changes_requested'):
            with self.subTest(verdict=verdict):
                body = f'Issue #12, PR #14, head {HEAD}\nVerdict: {verdict}\n  Evidence: "$PWD" → unchanged\n'
                launcher, *args = [Template(arg).substitute(pr_number='14', reviewed_head=HEAD, review_body=body)
                                   for arg in shlex.split(command)]
                self.assertEqual(launcher, APP)
                with (patch.object(sys, 'argv', [APP]),
                      patch('workflows.github.github_app.jwt', return_value='app-proof'),
                      patch('workflows.github.github_app.request', side_effect=self.request),
                      patch('workflows.github.github_app.subprocess.run', return_value=CompletedProcess([], 0)) as gh):
                    self.assertEqual(main(args, config_path=self.config_path), 0)
                self.assertEqual(gh.call_args.args[0], [
                    'gh', 'api', PR.lstrip('/') + '/reviews', '--method', 'POST',
                    '--raw-field', 'event=COMMENT', '--raw-field', f'commit_id={HEAD}',
                    '--raw-field', f'body={body}',
                ])
                self.assertEqual(gh.call_args.kwargs['env']['GH_TOKEN'], 'installation-fixture-token')
        self.assertIn('Recheck the current PR head after publication', prompt)
        self.assertIn('Return a short notification in your terminal result', prompt)
        self.assertIn('Leave the detailed review body in the PR review', prompt)

    def test_rendered_workflow_and_qualifying_write(self):
        for required in ('clean verdict', 'no outstanding actionable feedback', 'required checks',
                         'even if GitHub requires zero approvals', 'after checking collaborator access',
                         'Leave auto-merge and merge queue admission to the operator'):
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
