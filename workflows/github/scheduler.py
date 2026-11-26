"""Plan GitHub workflow dispatches from dashboard entries and verified observations."""

if __package__:
    from .merge_reviewed import SHA, mergeability_hold, positive_id, verified_pull
else:
    from merge_reviewed import SHA, mergeability_hold, positive_id, verified_pull


ATTENTION = 'Need attention'
APPROVAL = 'Awaiting human approval'
IMPLEMENTATION = 'In progress'
REVIEW = 'In review'
QUEUE = ' → Queue'
ACTIVE = {'implementation': IMPLEMENTATION, 'review': REVIEW}
PLACEMENTS = (None, ATTENTION, IMPLEMENTATION, REVIEW, IMPLEMENTATION + QUEUE, REVIEW + QUEUE, 'Done')
PULL_FIELDS = ('headRefOid', 'state', 'isDraft', 'mergeable', 'autoMergeRequest', 'isInMergeQueue')
PRE_SUBMISSION = {'invalid_parameters', 'missing_parameter', 'invalid_directory', 'directory_in_use',
                  'quota_account_changed', 'quota_unavailable', 'quota_behind_pace', 'quota_exhausted', 'deadline_elapsed'}


def pr_facts(installation, number):
    _, pull = verified_pull(installation, number)
    facts = {key: pull[key] for key in PULL_FIELDS}
    if (facts['state'] not in ('OPEN', 'CLOSED', 'MERGED') or type(facts['isDraft']) is not bool
            or facts['mergeable'] not in ('MERGEABLE', 'CONFLICTING', 'UNKNOWN')
            or type(facts['isInMergeQueue']) is not bool):
        raise ValueError('Unverified pull request facts')
    return dict(facts, number=number)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def matches(item, target, keys):
    return all(key in item and key in target and item[key] == target[key] for key in keys)


def feedback_ids(values):
    if not isinstance(values, list) or not all(text(item) for item in values) or len(set(values)) != len(values):
        raise ValueError('Feedback IDs must be unique nonempty strings')
    return values


def approval_hold(entry, pull, verdict):
    saved = entry.get('approval_hold')
    if saved is None or entry.get('feedback') == 'actionable':
        return None
    pr = saved.get('pr', entry.get('pr'))
    if not positive_id(pr) or pr != entry.get('pr') or verdict.get('verdict') != 'clean' or verdict.get('pr') != pr:
        return None
    head = saved.get('head') or verdict.get('head')
    if not sha(head) or head != verdict.get('head'):
        return None
    if pull and (pull['headRefOid'] != head or pull['state'] != 'OPEN'):
        return None
    return dict(pr=pr, head=head)


def verified_turn(attempt, evidence):
    pane, turn = evidence.get('pane_id'), evidence.get('turn_id')
    if not text(pane) or not text(turn):
        return None
    if attempt.get('pane_id') is not None and attempt['pane_id'] != pane:
        return None
    if attempt.get('thread_id') is not None and evidence.get('thread_id') != attempt['thread_id']:
        return None
    if evidence.get('working_directory') is not None and evidence['working_directory'] != attempt.get('working_directory'):
        return None
    if attempt['operation'] == 'steer' and (not text(attempt.get('pane_id')) or not text(attempt.get('thread_id'))
                                           or 'previous_turn_id' not in attempt
                                           or turn == attempt['previous_turn_id']):
        return None
    admitted = dict(attempt, pane_id=pane, turn_id=turn)
    if text(evidence.get('thread_id')):
        admitted['thread_id'] = evidence['thread_id']
    return admitted


def dispatch_outcome(attempt, receipt, recovery):
    """Classify a single recorded attempt, using only receipts correlated to it."""
    method = attempt['operation']
    if method not in ('run', 'steer'):
        raise ValueError('Unknown dispatch operation')
    if not receipt:
        receipt = {}
    result = receipt.get('result') or {}
    error = receipt.get('error') or {}
    if receipt.get('ok') is True:
        if method == 'run':
            accepted = result.get('admission') == 'accepted' and result.get('status', 'accepted') == 'accepted'
        else:
            accepted = result.get('status') == 'accepted' and result.get('admission', 'accepted') == 'accepted'
        if accepted and (turn := verified_turn(attempt, result)):
            return 'admitted', turn
    if receipt.get('ok') is False and method == 'run' and error.get('code') == 'already_started':
        if error.get('admission') == 'accepted' and (turn := verified_turn(attempt, error)):
            return 'admitted', turn

    if recovery:
        binding = ('working_directory',) if method == 'run' else ('pane_id', 'thread_id', 'previous_turn_id')
        if matches(recovery, attempt, ('issue', 'operation', 'worker', 'pr', 'head', *binding)):
            if recovery.get('state') == 'admitted' and (turn := verified_turn(attempt, recovery)):
                return 'admitted', turn
            if (recovery.get('state') == 'not_attempted' and (method == 'steer' or recovery.get('launch_cleared') is True)
                    and (not attempt.get('pane_id') or recovery.get('pane_id') == attempt['pane_id'])):
                return 'refused', None

    code = error.get('code')
    if receipt.get('ok') is False and error.get('admission') not in ('unknown', 'accepted'):
        # These engine refusals happen before provisioning/submission, or on the original idle steer pane.
        if code == 'capacity' and (not error.get('pane_id') or
                                   (method == 'steer' and error['pane_id'] == attempt.get('pane_id'))):
            return 'capacity', None
        if error.get('admission') == 'not_attempted':
            if method == 'run' and (error.get('pane_id') or code == 'partial_provision'):
                return 'retained_launch', None
            if not error.get('pane_id') or error['pane_id'] == attempt.get('pane_id'):
                return 'refused', None
        if (code in PRE_SUBMISSION or (code == 'not_idle' and method == 'steer')) and (
                not error.get('pane_id') or error['pane_id'] == attempt.get('pane_id')):
            return 'refused', None
    return 'unresolved', None


def finish_dispatch(decision, entry, blocker):
    attempt = entry['dispatch']
    if attempt['issue'] != entry['issue'] or attempt['kind'] not in ACTIVE:
        raise ValueError('Recorded dispatch does not identify the dashboard issue and work kind')
    outcome, admitted = dispatch_outcome(attempt, entry.get('receipt'), entry.get('recovery'))
    if outcome in ('unresolved', 'retained_launch'):
        # A receipt may identify a new run's pane before it identifies a turn.
        error = (entry.get('receipt') or {}).get('error') or {}
        if (attempt['operation'] == 'run' and not attempt.get('pane_id') and text(error.get('pane_id'))
                and (error.get('admission') in ('unknown', 'not_attempted') or error.get('code') == 'outcome_unknown')):
            decision['observed_pane_id'] = error['pane_id']
            if text(error.get('thread_id')):
                decision['observed_thread_id'] = error['thread_id']
        reason = 'Retained launch pane' if outcome == 'retained_launch' else 'Unresolved dispatch'
        return dict(decision, placement=ATTENTION, status=reason, action='reconcile')
    decision['clear_dispatch'] = True
    if admitted:
        batch = feedback_ids(attempt['feedback_ids'])
        pending = feedback_ids(entry['pending_feedback'])
        assigned = feedback_ids(entry.get('assigned_feedback', []))
        return dict(decision, placement=ATTENTION if blocker else ACTIVE[attempt['kind']], status=blocker,
                    action='follow', admitted=admitted, pending_feedback=[item for item in pending if item not in batch],
                    assigned_feedback=list(dict.fromkeys(assigned + batch)))
    if outcome == 'capacity' and not blocker:
        return dict(decision, placement=ACTIVE[attempt['kind']] + QUEUE,
                    status='Waiting for a worker slot', action='wait_capacity')
    return dict(decision, placement=ATTENTION, status=blocker or 'Worker refused dispatch', action='refresh')


def target_work(entry, pull, current_verdict, names, new):
    requested = entry.get('pending_work')
    initial_implementation = not pull and (new or entry.get('placement') == IMPLEMENTATION + QUEUE)
    if requested == 'implementation' or entry.get('feedback') == 'actionable' or initial_implementation:
        kind = 'implementation'
    elif pull and (requested == 'review' or not current_verdict):
        kind = 'review'
    else:
        return None
    return dict(kind=kind, worker=names[kind], pr=entry.get('pr'), head=pull['headRefOid'] if pull else None)


def ready_handoff(target, entry):
    handoff = entry.get('handoff') or {}
    if not matches(handoff, target, ('kind', 'pr', 'head')):
        return None
    batch = feedback_ids(handoff['feedback_ids'])
    if (not set(batch) <= set(feedback_ids(entry['pending_feedback']))
            or (entry.get('feedback') == 'actionable' and not batch)):
        return None
    dispatch = dict(target, issue=entry['issue'], feedback_ids=batch, operation='run')
    if pane := handoff.get('steer'):
        if (pane.get('worker') == target['worker'] and pane.get('issue') == entry['issue']
                and pane.get('lock') == 'unlocked' and pane.get('state') in ('idle', 'done')
                and pane.get('pending_closure') is False and pane.get('quota_ready') is True
                and text(pane.get('pane_id')) and text(pane.get('thread_id')) and 'turn_id' in pane
                and (pane['turn_id'] is None or text(pane['turn_id']))):
            dispatch.update(operation='steer', pane_id=pane['pane_id'], thread_id=pane['thread_id'],
                            previous_turn_id=pane['turn_id'])
    return dispatch


def decide(entry, capacity, names, event, attempted, new):
    pr = entry.get('pr')
    pull = entry.get('pull')
    if pull and (not positive_id(pr) or pull['number'] != pr or not sha(pull['headRefOid'])):
        raise ValueError('Pull facts must identify the tracked PR and its full head SHA')
    verdict = entry.get('verdict') or {}
    hold = approval_hold(entry, pull, verdict)
    conflict = mergeability_hold(pull['mergeable']) if pull and pull['state'] == 'OPEN' else None
    blocker = entry.get('blocker')
    if pull and (pull['isInMergeQueue'] is True or pull['autoMergeRequest'] is not None):
        blocker = blocker or 'PR is queued or scheduled for merge'
    decision = dict(issue=entry['issue'], placement=entry.get('placement'), action='keep', approval_hold=hold,
                    merge_blocker=conflict, pending_work=None)
    if entry.get('approval_hold') is not None and hold is None:
        decision['status'] = None
    if entry.get('dispatch') is not None:
        return finish_dispatch(decision, entry, blocker)
    if decision['placement'] == 'Done' or (pull and pull['state'] == 'MERGED'):
        return dict(decision, placement='Done', action='close_panes')
    if entry.get('active_assignment') is not None:
        return dict(decision, placement=ATTENTION, action='blocked', status=blocker) if blocker else decision
    if entry.get('eligible') is not True or (new and event != 'periodic'):
        return decision
    if blocker:
        return dict(decision, placement=ATTENTION, action='blocked', status=blocker)
    if pr is not None and (not pull or pull['state'] != 'OPEN' or pull['isDraft'] is not False):
        return dict(decision, placement=ATTENTION, action='blocked', status='Awaiting a reviewable PR')
    current_verdict = bool(pull and verdict.get('pr') == pr and verdict.get('head') == pull['headRefOid']
                           and verdict.get('verdict') in ('clean', 'changes_requested'))
    target = target_work(entry, pull, current_verdict, names, new)
    if pr is not None and entry.get('feedback') not in ('clear', 'actionable'):
        return dict(decision, placement=ATTENTION, action='blocked', status='Awaiting feedback verification')
    if target is None:
        if current_verdict and verdict['verdict'] == 'clean':
            if entry.get('pending_feedback'):
                return dict(decision, placement=ATTENTION, action='blocked', status='Awaiting feedback verification')
            merge_check = entry.get('merge_check') or {}
            candidate = dict(pr=pr, head=pull['headRefOid'])
            if not matches(merge_check, candidate, ('pr', 'head')):
                merge_check = {}
            uncertain = merge_check.get('outcome') == 'unconfirmed'
            reason = merge_check.get('reason')
            if reason in ('Merge conflict', 'Awaiting mergeability verification'):
                reason = None  # The snapshot supersedes past mergeability decisions.
            status = ('Merge outcome unconfirmed' if uncertain else conflict or reason or
                      (APPROVAL if hold else 'Awaiting merge verification'))
            decision.update(placement=ATTENTION, status=status)
            if event == 'periodic' and not (conflict or uncertain):
                decision['merge_candidate'] = candidate
        return decision

    decision['pending_work'] = target
    if entry['issue'] in attempted:
        return decision
    dispatch = ready_handoff(target, entry)
    if dispatch is None:
        return dict(decision, placement=ATTENTION, action='prepare_handoff', status='Handoff incomplete')
    if capacity['available'] == 0:
        return dict(decision, placement=ACTIVE[target['kind']] + QUEUE, action='wait_capacity',
                    status='Waiting for a worker slot')
    worker = capacity['workers'].get(target['worker']) or {}
    # Steer readiness comes from the verified idle binding plus quota preflight. Capacity's worker
    # readiness also checks the default launch checkout, which is only used by a new run.
    if dispatch['operation'] == 'run' and worker.get('ready') is not True:
        return dict(decision, placement=ATTENTION, action='blocked', status=worker.get('hold') or 'Worker unavailable')
    decision.update(action='ready', dispatch=dispatch)
    return decision


def result_action(event, report):
    if event == 'periodic':
        return None
    if not report or not text(report.get('pane_id')) or not text(report.get('turn_id')):
        raise ValueError('A worker result must identify its exact pane and turn')
    ids = {key: report[key] for key in ('pane_id', 'turn_id')}
    if report.get('recorded') is not True:
        return dict(action='record_result', **ids)
    ack = report.get('acknowledgment') or {}
    result = ack.get('result') or {}
    if (ack.get('ok') is not True or not matches(result, ids, ('pane_id', 'turn_id'))
            or result.get('delivery') != 'read' or result.get('attention_id') is not None):
        return dict(action='mark_read', **ids)
    return None


def plan(request, *, implementer, reviewer):
    """Return placement updates and at most one next dispatch, without performing either."""
    event = request['event']
    if event not in ('periodic', 'worker_result'):
        raise ValueError('Unknown scheduling event')
    pending_result = result_action(event, request.get('worker_result'))
    entries = request.get('entries', []) if pending_result else request['entries']
    if pending_result:
        # A result may be the first proof of an interrupted admission. Settle its journal
        # before the manager records the result, but hold all new work until acknowledgment.
        entries = [entry for entry in entries if entry.get('dispatch') is not None]
        if not entries:
            return dict(worker_result=pending_result, decisions=[], dispatch=None)

    capacity = request['capacity']
    if type(capacity['available']) is not int or capacity['available'] < 0 or not isinstance(capacity['workers'], dict):
        raise ValueError('A verified capacity observation is required')
    by_issue = {entry['issue']: entry for entry in entries}
    if (len(by_issue) != len(entries) or not all(positive_id(key) for key in by_issue)
            or any(entry.get('placement') not in PLACEMENTS for entry in entries)):
        raise ValueError('Entries must have unique issue numbers and valid dashboard placements')
    queued = request['queued_at_start']
    attempted = request.get('attempted', [])
    intake = request.get('new_intake', [])
    if (not all(positive_id(issue) for issue in queued + attempted + intake) or len(set(queued)) != len(queued)
            or set(queued).intersection(intake)):
        raise ValueError('Turn-start queues, new intake and attempts must use valid issue IDs')
    if not text(implementer) or not text(reviewer):
        raise ValueError('Configured implementer and reviewer names are required')
    names = dict(implementation=implementer, review=reviewer)
    new = set(intake) | {issue for issue, entry in by_issue.items() if entry.get('placement') is None}
    order = dict.fromkeys([*queued, *(issue for issue in by_issue if issue not in new), *by_issue])
    decisions = [decide(by_issue[issue], capacity, names, event, attempted, issue in new)
                 for issue in order if issue in by_issue]
    if pending_result:
        return dict(worker_result=pending_result, decisions=decisions, dispatch=None)
    # Save reconciled receipts before selecting a dispatch against refreshed capacity and entries.
    dispatch = None
    if not any(decision.get('clear_dispatch') for decision in decisions):
        dispatch = next((decision['dispatch'] for decision in decisions if decision['action'] == 'ready'), None)
    for decision in decisions:
        decision.pop('dispatch', None)
    return dict(worker_result=None, decisions=decisions, dispatch=dispatch)
