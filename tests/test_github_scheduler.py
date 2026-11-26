"""Exercise GitHub workflow scheduling with disposable dashboard and tool observations."""

from copy import deepcopy
import unittest

from workflows.github.scheduler import plan


H1 = 'a' * 40
H2 = 'b' * 40
IMPLEMENTER = 'impl-prod'
REVIEWER = 'review-prod'
CHECKOUT = '/fixture/checkout'


def facts(head=H2, *, pr=26, mergeable='MERGEABLE', **changes):
    return dict(number=pr, headRefOid=head, state='OPEN', isDraft=False, mergeable=mergeable,
                autoMergeRequest=None, isInMergeQueue=False) | changes


def handoff(kind='review', *, pr=26, head=H2, feedback=(), **changes):
    worker = REVIEWER if kind == 'review' else IMPLEMENTER
    return dict(kind=kind, pr=pr, head=head, feedback_ids=list(feedback),
                checkout=dict(worker=worker, working_directory=CHECKOUT, ready=True, hold=None)) | changes


def issue(number=25, **changes):
    return dict(issue=number, placement='Need attention', eligible=True, pr=26, pull=facts(),
                feedback='clear', pending_feedback=[], handoff=handoff()) | changes


def result(*, recorded=True, acknowledgment=None):
    return dict(pane_id='w1:result', turn_id='finished', recorded=recorded, acknowledgment=acknowledgment)


def acknowledged(**changes):
    return dict(ok=True, result=dict(pane_id='w1:result', turn_id='finished', delivery='read') | changes)


def schedule(*entries, slots=1, queued=(), attempted=(), intake=(), report=None, **worker_holds):
    workers = {name: dict(ready=bool(slots), hold=None if slots else 'capacity') for name in (IMPLEMENTER, REVIEWER)}
    workers.update(worker_holds)
    return plan(dict(event='periodic' if report is None else 'worker_result', worker_result=report,
                     entries=entries, queued_at_start=list(queued), attempted=list(attempted), new_intake=list(intake),
                     capacity=dict(available=slots, workers=workers)), implementer=IMPLEMENTER, reviewer=REVIEWER)


def dispatch_issue(kind='review', **changes):
    worker = REVIEWER if kind == 'review' else IMPLEMENTER
    dispatch = dict(issue=25, kind=kind, worker=worker, operation='run', pr=26, head=H2,
                    working_directory='/fixture/pr-26', feedback_ids=['sent'])
    return issue(dispatch=dispatch, pending_feedback=['sent', 'later']) | changes


class GitHubScheduler(unittest.TestCase):
    def test_changed_head_releases_approval_hold_and_admits_or_queues_review(self):
        verdict = dict(pr=26, head=H1, verdict='clean')
        for hold in (dict(pr=26, head=H1), dict(pr=26), {}):
            with self.subTest(hold=hold):
                old = issue(pull=facts(H1), verdict=verdict, approval_hold=hold)
                old_plan = schedule(old)
                self.assertIsNone(old_plan['dispatch'])
                self.assertEqual(old_plan['decisions'][0]['approval_hold'], dict(pr=26, head=H1))
                self.assertEqual(old_plan['decisions'][0]['status'], 'Awaiting human approval')
                self.assertEqual(old_plan['decisions'][0]['merge_candidate'], dict(pr=26, head=H1))

                new = old | dict(pull=facts(H2))
                waiting = schedule(new, slots=0)
                self.assertIsNone(waiting['dispatch'])
                queued = waiting['decisions'][0]
                self.assertEqual(queued['action'], 'wait_capacity')
                self.assertEqual(queued['placement'], 'In review → Queue')
                self.assertIsNone(queued['approval_hold'])
                self.assertNotIn('merge_candidate', queued)

                ready = schedule(new)
                self.assertEqual(ready['dispatch'], dict(issue=25, kind='review', pr=26, head=H2,
                                                         worker=REVIEWER, operation='run', feedback_ids=[],
                                                         working_directory=CHECKOUT))
                self.assertEqual(ready['decisions'][0]['placement'], 'Need attention')
                self.assertIsNone(ready['decisions'][0]['approval_hold'])
                admitted = schedule(new | dict(dispatch=dict(ready['dispatch'], working_directory='/fixture/h2'),
                                                receipt=dict(ok=True, result=dict(admission='accepted',
                                                                                 pane_id='w1:new', turn_id='h2-turn'))),
                                    slots=0, attempted=(25,))['decisions'][0]
                self.assertEqual(admitted['placement'], 'In review')
                self.assertEqual(admitted['admitted']['head'], H2)
                self.assertTrue(admitted['clear_dispatch'])

    def test_legacy_hold_requires_a_verified_clean_verdict_for_the_same_pr(self):
        for verdict in (None, dict(pr=42, head=H2, verdict='clean'), dict(pr=26, head='b1', verdict='clean'),
                        dict(pr=26, head=H2, verdict='changes_requested')):
            with self.subTest(verdict=verdict):
                self.assertIsNone(schedule(issue(approval_hold={}, verdict=verdict))['decisions'][0]['approval_hold'])
        changed_pr = issue(pr=27, pull=facts(pr=27), handoff=handoff(pr=27),
                           approval_hold=dict(pr=26, head=H2), verdict=dict(pr=26, head=H2, verdict='clean'))
        decision = schedule(changed_pr)
        self.assertIsNone(decision['decisions'][0]['approval_hold'])
        self.assertEqual(decision['dispatch']['pr'], 27)

    def test_new_actionable_feedback_releases_same_head_hold_for_implementation(self):
        held = issue(approval_hold=dict(pr=26, head=H2), verdict=dict(pr=26, head=H2, verdict='clean'),
                     feedback='actionable', pending_feedback=['finding-1', 'finding-2'])
        refresh = schedule(held)
        self.assertIsNone(refresh['dispatch'])
        self.assertEqual(refresh['decisions'][0]['action'], 'prepare_handoff')
        self.assertEqual(refresh['decisions'][0]['pending_work']['kind'], 'implementation')
        self.assertIsNone(refresh['decisions'][0]['approval_hold'])
        ready = schedule(held | dict(handoff=handoff('implementation', feedback=['finding-1'])))
        self.assertEqual(ready['dispatch']['worker'], IMPLEMENTER)
        self.assertEqual(ready['dispatch']['head'], H2)
        self.assertEqual(ready['dispatch']['feedback_ids'], ['finding-1'])
        self.assertNotIn('merge_candidate', ready['decisions'][0])

    def test_confirmed_conflict_unknown_and_later_clear_snapshot(self):
        clean = issue(verdict=dict(pr=26, head=H2, verdict='clean'))
        for observed, reason in (('CONFLICTING', 'Merge conflict'), ('UNKNOWN', 'Awaiting mergeability verification')):
            with self.subTest(observed=observed):
                current = facts(mergeable=observed)
                blocked = schedule(clean | dict(pull=current))['decisions'][0]
                self.assertEqual(blocked['merge_blocker'], reason)
                self.assertEqual(blocked['status'], reason)
                self.assertNotIn('merge_candidate', blocked)
                self.assertEqual(schedule(issue(pull=current))['dispatch']['head'], H2)
        cleared = schedule(clean | dict(pull=facts(mergeable='MERGEABLE'),
                                        merge_check=dict(pr=26, head=H2, reason='Merge conflict')))['decisions'][0]
        self.assertIsNone(cleared['merge_blocker'])
        self.assertEqual(cleared['status'], 'Awaiting merge verification')
        self.assertEqual(cleared['merge_candidate'], dict(pr=26, head=H2))

    def test_draft_allows_implementation_and_holds_review_and_merge_on_both_paths(self):
        review = issue(placement='In review → Queue', pull=facts(isDraft=True))
        clean_verdict = dict(pr=26, head=H2, verdict='clean')
        for report in (None, result(acknowledgment=acknowledged())):
            with self.subTest(report=report):
                follow_up = review | dict(placement='In progress → Queue', pending_work='implementation',
                                          feedback='actionable', pending_feedback=['fix'],
                                          handoff=handoff('implementation', feedback=['fix']))
                dispatch = schedule(follow_up, queued=(25,), report=report)['dispatch']
                self.assertEqual((dispatch['pr'], dispatch['head'], dispatch['worker']), (26, H2, IMPLEMENTER))
                self.assertEqual(dispatch['feedback_ids'], ['fix'])
                for entry in (review, review | dict(verdict=clean_verdict), follow_up | dict(pull=facts(state='CLOSED'))):
                    held = schedule(entry, queued=(25,), report=report)
                    self.assertIsNone(held['dispatch'])
                    self.assertEqual(held['decisions'][0]['status'], 'Awaiting a reviewable PR')
                    self.assertNotIn('merge_candidate', held['decisions'][0])

    def test_checkout_preflight_preserves_priority_and_real_holds_on_both_paths(self):
        queued = issue(placement='In review → Queue', handoff=handoff(checkout=None))
        other = issue(12)
        for report in (None, result(acknowledgment=acknowledged())):
            with self.subTest(report=report):
                waiting = schedule(other, queued, queued=(25,), report=report)
                self.assertIsNone(waiting['dispatch'])
                self.assertEqual(waiting['prepare_checkout'], dict(issue=25, kind='review', worker=REVIEWER, pr=26, head=H2))
                for reason in ('directory_in_use', 'invalid_directory', 'quota_behind_pace', 'worker_unavailable'):
                    checkout = dict(worker=REVIEWER, working_directory=CHECKOUT, ready=False, hold=reason)
                    held = schedule(other, queued | dict(handoff=handoff(checkout=checkout)), queued=(25,), report=report)
                    self.assertEqual(held['decisions'][0]['status'], reason)
                    self.assertEqual(held['dispatch']['issue'], 12)
                wrong_worker = queued | dict(handoff=handoff(checkout=handoff('implementation')['checkout']))
                self.assertEqual(schedule(wrong_worker, report=report)['prepare_checkout']['worker'], REVIEWER)
                stale = dict(worker=REVIEWER, working_directory=CHECKOUT, ready=False, hold='capacity')
                refresh = schedule(queued | dict(handoff=handoff(checkout=stale)), report=report)
                self.assertEqual(refresh['prepare_checkout']['issue'], 25)

    def test_unconfirmed_checked_merge_and_pending_feedback_hold_the_merge_candidate(self):
        clean = issue(verdict=dict(pr=26, head=H2, verdict='clean'))
        uncertain = dict(pr=26, head=H2, outcome='unconfirmed')
        for fields, reason in ((dict(merge_check=uncertain), 'Merge outcome unconfirmed'),
                               (dict(pending_feedback=['unchecked']), 'Awaiting feedback verification')):
            with self.subTest(fields=fields):
                decision = schedule(clean | fields)['decisions'][0]
                self.assertEqual(decision['status'], reason)
                self.assertNotIn('merge_candidate', decision)

    def test_old_head_handoff_must_be_rebuilt(self):
        decision = schedule(issue(handoff=handoff(head=H1)))
        self.assertIsNone(decision['dispatch'])
        self.assertEqual(decision['decisions'][0]['pending_work']['head'], H2)
        self.assertEqual(decision['decisions'][0]['action'], 'prepare_handoff')

    def test_worker_result_must_be_recorded_and_acknowledged_on_exact_turn(self):
        for report, action in ((result(recorded=False, acknowledgment=acknowledged()), 'record_result'),
                               (result(), 'mark_read'), (result(acknowledgment=acknowledged(turn_id='other')), 'mark_read'),
                               (result(acknowledgment=acknowledged(attention_id='escalation')), 'mark_read'),
                               (result(acknowledgment=dict(ok=False)), 'mark_read')):
            with self.subTest(report=report):
                held = schedule(issue(), report=report)
                self.assertEqual(held['worker_result'], dict(action=action, pane_id='w1:result', turn_id='finished'))
                self.assertEqual(held['decisions'], [])
                self.assertIsNone(held['dispatch'])

    def test_unread_result_can_reconcile_its_original_dispatch_before_acknowledgment(self):
        entry = dispatch_issue()
        recovery = dict(entry['dispatch'], state='admitted', pane_id='w1:result', turn_id='finished')
        unread = schedule(entry | dict(recovery=recovery), issue(12), report=result(recorded=False))
        self.assertEqual(unread['worker_result']['action'], 'record_result')
        self.assertIsNone(unread['dispatch'])
        admitted, = unread['decisions']
        self.assertEqual(admitted['action'], 'follow')
        self.assertEqual(admitted['admitted']['turn_id'], 'finished')
        self.assertTrue(admitted['clear_dispatch'])

    def test_both_event_paths_give_existing_queues_the_first_slot(self):
        queued = issue(2, placement='In progress → Queue', pr=None, pull=None, pending_work='implementation',
                       handoff=handoff('implementation', pr=None, head=None))
        tracked = issue(3, placement='In review → Queue')
        new = issue(1, placement=None, pr=None, pull=None, handoff=handoff('implementation', pr=None, head=None))
        for report in (None, result(acknowledgment=acknowledged())):
            with self.subTest(report=report):
                selected = schedule(new, tracked, queued, report=report, queued=(2,))
                self.assertEqual(selected['dispatch']['issue'], 2)
                self.assertEqual(selected['dispatch']['worker'], IMPLEMENTER)
                after = schedule(new, tracked, queued, slots=0, report=report, queued=(2,), attempted=(2,))
                self.assertIsNone(after['dispatch'])
                self.assertEqual(next(row for row in after['decisions'] if row['issue'] == 3)['placement'], 'In review → Queue')
                blocked = queued | dict(blocker='Worker escalation')
                self.assertEqual(schedule(new, tracked, blocked, report=report, queued=(2,))['dispatch']['issue'], 3)
        self.assertIsNone(schedule(new, report=result(acknowledgment=acknowledged()))['dispatch'])
        self.assertEqual(schedule(new)['dispatch']['issue'], 1)

    def test_new_intake_keeps_its_priority_after_dashboard_entry_is_created(self):
        new = issue(1, placement='In progress → Queue', pr=None, pull=None,
                    handoff=handoff('implementation', pr=None, head=None))
        tracked = issue(2)
        self.assertEqual(schedule(new, tracked, intake=(1,))['dispatch']['issue'], 2)
        self.assertEqual(schedule(new, intake=(1,))['dispatch']['worker'], IMPLEMENTER)
        self.assertIsNone(schedule(new, intake=(1,), report=result(acknowledgment=acknowledged()))['dispatch'])

    def test_configured_reviewer_routes_ready_and_queued_reviews_and_only_matching_steers(self):
        pane = dict(issue=25, worker=REVIEWER, pane_id='w1:review', thread_id='thread', turn_id='previous',
                    lock='unlocked', state='done', pending_closure=False, quota_ready=True)
        for placement in ('Need attention', 'In review → Queue'):
            with self.subTest(placement=placement):
                work = issue(placement=placement, handoff=handoff(steer=pane))
                dispatch = schedule(work)['dispatch']
                self.assertEqual(dispatch['operation'], 'steer')
                self.assertEqual(dispatch['worker'], REVIEWER)
                self.assertEqual(dispatch['previous_turn_id'], 'previous')
                wrong_worker = work | dict(handoff=handoff(steer=dict(pane, worker=IMPLEMENTER)))
                self.assertEqual(schedule(wrong_worker)['dispatch']['operation'], 'run')
                self.assertEqual(schedule(wrong_worker)['dispatch']['worker'], REVIEWER)

    def test_capacity_refusals_clear_attempts_and_keep_pending_feedback(self):
        for method, kind, placement in (('run', 'review', 'In review → Queue'),
                                        ('steer', 'implementation', 'In progress → Queue')):
            with self.subTest(method=method):
                entry = dispatch_issue(kind, receipt=dict(ok=False, error=dict(code='capacity')))
                if method == 'steer':
                    entry['dispatch'].update(operation='steer', pane_id='w1:steer',
                                             thread_id='thread', previous_turn_id='old')
                outcome = schedule(entry, issue(20), queued=(25,))
                queued = outcome['decisions'][0]
                self.assertIsNone(outcome['dispatch'])
                self.assertTrue(queued['clear_dispatch'])
                self.assertEqual(queued['placement'], placement)
                self.assertNotIn('pending_feedback', queued)
                self.assertNotIn('assigned_feedback', queued)
                self.assertEqual(entry['pending_feedback'], ['sent', 'later'])
                reset = entry | dict(dispatch=None, receipt=None, placement=placement)
                self.assertIsNone(schedule(reset, attempted=(25,))['dispatch'])

    def test_verified_admission_assigns_only_the_admitted_batch_even_with_a_blocker(self):
        first = dispatch_issue(assigned_feedback=['earlier'], blocker='Worker escalation')
        for receipt in (dict(ok=True, result=dict(admission='accepted', pane_id='pane', turn_id='turn')),
                        dict(ok=False, error=dict(code='already_started', admission='accepted', pane_id='pane', turn_id='turn'))):
            with self.subTest(receipt=receipt):
                outcome = schedule(first | dict(receipt=receipt))['decisions'][0]
                self.assertEqual(outcome['placement'], 'Need attention')
                self.assertEqual(outcome['status'], 'Worker escalation')
                self.assertEqual(outcome['admitted']['turn_id'], 'turn')
                self.assertEqual(outcome['pending_feedback'], ['later'])
                self.assertEqual(outcome['assigned_feedback'], ['earlier', 'sent'])

    def test_uncertain_and_mismatched_run_receipts_require_matching_recovery(self):
        entry = dispatch_issue()
        ambiguous = dict(ok=False, error=dict(code='outcome_unknown', admission='unknown', pane_id='pane',
                                              thread_id='thread'))
        options = (None, ambiguous, dict(ok=False, error=dict(code='capacity', admission='unknown')),
                   dict(ok=True, result=dict(admission='accepted', pane_id='pane')),
                   dict(ok=True, result=dict(admission='accepted', status='refused', pane_id='pane', turn_id='new')))
        for receipt in options:
            with self.subTest(receipt=receipt):
                held = schedule(entry | dict(receipt=receipt))
                self.assertIsNone(held['dispatch'])
                self.assertEqual(held['decisions'][0]['action'], 'reconcile')
                self.assertNotIn('clear_dispatch', held['decisions'][0])
                if receipt == ambiguous:
                    self.assertEqual(held['decisions'][0]['observed_pane_id'], 'pane')
                    self.assertEqual(held['decisions'][0]['observed_thread_id'], 'thread')
        entry['dispatch'].update(pane_id='pane', thread_id='thread')
        recovered = dict(entry['dispatch'], state='admitted', turn_id='new')
        for mismatch in (dict(head=H1), dict(issue=24), dict(working_directory='/different'), dict(pane_id='wrong'),
                         dict(thread_id='wrong'), dict(turn_id=None)):
            with self.subTest(mismatch=mismatch):
                held = schedule(entry | dict(recovery=recovered | mismatch))['decisions'][0]
                self.assertEqual(held['action'], 'reconcile')
        outcome = schedule(entry | dict(receipt=ambiguous, recovery=recovered))['decisions'][0]
        self.assertEqual(outcome['action'], 'follow')
        self.assertEqual(outcome['admitted']['turn_id'], 'new')

    def test_steer_receipt_and_recovery_require_a_new_turn_on_the_original_binding(self):
        entry = dispatch_issue()
        entry['dispatch'].update(operation='steer', pane_id='pane', thread_id='thread', previous_turn_id='old')
        response = dict(status='accepted', pane_id='pane', thread_id='thread', turn_id='new')
        for mismatch in (dict(turn_id='old'), dict(thread_id='wrong'), dict(pane_id='wrong')):
            with self.subTest(mismatch=mismatch):
                held = schedule(entry | dict(receipt=dict(ok=True, result=response | mismatch)))['decisions'][0]
                self.assertEqual(held['action'], 'reconcile')
        self.assertEqual(schedule(entry | dict(receipt=dict(ok=True, result=response)))['decisions'][0]['action'], 'follow')
        recovery = dict(entry['dispatch'], state='admitted', turn_id='old')
        self.assertEqual(schedule(entry | dict(recovery=recovery))['decisions'][0]['action'], 'reconcile')
        recovery['turn_id'] = 'recovered'
        self.assertEqual(schedule(entry | dict(recovery=recovery))['decisions'][0]['admitted']['turn_id'], 'recovered')

    def test_not_attempted_retained_launch_must_be_cleared_before_retry(self):
        entry = dispatch_issue(receipt=dict(ok=False, error=dict(admission='not_attempted', code='worker_unavailable',
                                                                pane_id='retained')))
        held = schedule(entry)['decisions'][0]
        self.assertEqual(held['status'], 'Retained launch pane')
        entry['dispatch']['pane_id'] = held['observed_pane_id']
        recovery = dict(entry['dispatch'], state='not_attempted')
        self.assertEqual(schedule(entry | dict(recovery=recovery))['decisions'][0]['action'], 'reconcile')
        outcome = schedule(entry | dict(recovery=dict(recovery, launch_cleared=True)))
        self.assertTrue(outcome['decisions'][0]['clear_dispatch'])
        self.assertIsNone(outcome['dispatch'])

    def test_plan_rejects_duplicate_entries_and_unmatched_pull_facts(self):
        with self.assertRaisesRegex(ValueError, 'unique issue numbers'):
            schedule(issue(), issue())
        with self.assertRaisesRegex(ValueError, 'tracked PR'):
            schedule(issue(pr=27))

    def test_pending_receipt_uses_original_head_and_leaves_current_facts_intact(self):
        entry = dispatch_issue(receipt=dict(ok=True, result=dict(admission='accepted', pane_id='old-pane', turn_id='old')))
        entry['dispatch']['head'] = H1
        before = deepcopy(entry)
        admitted = schedule(entry)['decisions'][0]['admitted']
        self.assertEqual(admitted['head'], H1)
        self.assertEqual(entry, before)
