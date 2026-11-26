"""Verify the bundled workflow's merge conditions and submit a SHA-pinned merge."""

from datetime import datetime
import re
from urllib.parse import quote


SHA = re.compile(r'[0-9a-f]{40}')
HUMAN_LOGIN = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*')
ACCOUNT_LOGIN = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*(?:\[bot\])?')
METHODS = {'merge': 'mergeCommitAllowed', 'squash': 'squashMergeAllowed', 'rebase': 'rebaseMergeAllowed'}
PULL_REQUEST = '''
query ProjectManagerPullRequest($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed
    pullRequest(number: $number) {
      state isDraft headRefOid baseRefName author { login }
      mergeable mergeStateStatus reviewDecision
      autoMergeRequest { enabledAt }
      isInMergeQueue isMergeQueueEnabled
    }
  }
}
'''
EVIDENCE_ERRORS = (AttributeError, KeyError, OSError, TypeError, ValueError)


def add_arguments(parser):
    parser.add_argument('--pr', required=True, type=int)
    parser.add_argument('--reviewed-head', required=True)
    parser.add_argument('--worker-verdict')
    parser.add_argument('--feedback-clear', action='store_true')
    parser.add_argument('--human-logins', default='')
    parser.add_argument('--method', required=True, choices=METHODS)


def hold(reason):
    return {'status': 'Need attention', 'reason': reason}


def verified_pull(installation, number):
    if not positive_id(number) or number > 2**31 - 1:
        raise ValueError('A numeric PR number is required')
    owner, name = installation.repository.split('/')
    repository = installation.graphql(PULL_REQUEST, dict(owner=owner, name=name, number=number))['repository']
    if repository['nameWithOwner'].casefold() != installation.repository.casefold():
        raise ValueError('Unverified repository')
    pull = repository['pullRequest']
    if not SHA.fullmatch(pull['headRefOid']):
        raise ValueError('Unverified head')
    return repository, pull


def mergeability_hold(value):
    if value == 'MERGEABLE':
        return None
    if value == 'CONFLICTING':
        return 'Merge conflict'
    return 'Awaiting mergeability verification'


def pull_snapshot(installation, args):
    repository, pull = verified_pull(installation, args.pr)
    head = pull['headRefOid']
    if head != args.reviewed_head:
        return pull, {'status': 'Head changed', 'head': head}
    if pull['state'] != 'OPEN' or pull['isDraft'] is not False:
        return pull, hold('Pull request must be open and ready for review')
    if pull['autoMergeRequest'] is not None or pull['isInMergeQueue'] is not False:
        return pull, hold('Pull request is queued or already scheduled for merge')
    if pull['isMergeQueueEnabled'] is not False:
        return pull, hold('The base branch requires a merge queue')
    if reason := mergeability_hold(pull['mergeable']):
        return pull, hold(reason)
    base = pull['baseRefName']
    author = pull['author']['login'].lower()
    if not base or not ACCOUNT_LOGIN.fullmatch(author):
        raise ValueError('Unverified base or author')
    if repository[METHODS[args.method]] is not True:
        return pull, hold('Merge method is disabled by the repository')
    for rule in installation.pages('rules/branches/' + quote(base, safe='')):
        kind = rule['type']
        if not isinstance(kind, str) or not kind:
            raise ValueError('Unverified branch rules')
        if kind == 'merge_queue':
            return pull, hold('The base branch requires a merge queue')
        if kind == 'required_linear_history' and args.method == 'merge':
            return pull, hold('The base branch requires linear history')
        if kind == 'pull_request':
            allowed = rule['parameters'].get('allowed_merge_methods')
            if allowed is not None:
                if not isinstance(allowed, list) or not all(method in METHODS for method in allowed):
                    raise ValueError('Unverified branch merge methods')
                if args.method not in allowed:
                    return pull, hold('Merge method is disabled by a branch rule')
    if pull['reviewDecision'] == 'REVIEW_REQUIRED':
        return pull, awaiting_approval(args)
    if (pull['mergeStateStatus'] != 'CLEAN'
            or pull['reviewDecision'] not in (None, 'APPROVED')):
        return pull, hold('GitHub has not confirmed required checks, reviews and merge rules')
    return pull, None


def positive_id(value):
    return type(value) is int and value > 0


def awaiting_approval(args):
    return dict(status='Awaiting human approval', pr=args.pr, head=args.reviewed_head)


def approvals(installation, pull_path, humans, head):
    latest, logins, accounts, seen = {}, {}, {}, set()
    last_submitted = None
    for review in installation.pages(pull_path + '/reviews'):
        review_id, state = review['id'], review['state']
        if not positive_id(review_id) or review_id in seen:
            raise ValueError('Unverified review history')
        seen.add(review_id)
        if state in ('COMMENTED', 'PENDING'):
            continue
        if state not in ('APPROVED', 'DISMISSED', 'CHANGES_REQUESTED'):
            raise ValueError('Unverified review state')
        user = review['user']
        login, user_id, account_type = user['login'].lower(), user['id'], user['type']
        if (not ACCOUNT_LOGIN.fullmatch(login) or not positive_id(user_id)
                or account_type not in ('User', 'Bot')
                or logins.setdefault(user_id, (login, account_type)) != (login, account_type)
                or accounts.setdefault(login, user_id) != user_id):
            raise ValueError('Unverified review identity')
        submitted = datetime.fromisoformat(review['submitted_at'].replace('Z', '+00:00'))
        if (submitted.utcoffset() is None or (last_submitted and submitted < last_submitted)
                or not SHA.fullmatch(review['commit_id'])):
            raise ValueError('Unverified review submission')
        # GitHub returns submitted reviews in chronological order.
        last_submitted = submitted
        latest[login] = review
    if any(review['state'] == 'CHANGES_REQUESTED' for review in latest.values()):
        return set(), True
    return {
        (login, review['user']['id'], review['id'])
        for login, review in latest.items()
        if login in humans and review['user']['type'] == 'User'
        and review['state'] == 'APPROVED' and review['commit_id'] == head
    }, False


def find_reviewer(installation, candidates):
    for login, user_id, review_id in sorted(candidates):
        grant = installation.api('GET', f'collaborators/{login}/permission')
        if grant['permission'] in ('read', 'none'):
            continue
        user = grant['user']
        if (grant['permission'] not in ('write', 'admin') or user['type'] != 'User'
                or user['login'].lower() != login or not positive_id(user['id']) or user['id'] != user_id):
            raise ValueError('Unverified collaborator identity')
        return login, user_id, review_id
    return None


def merge_reviewed(installation, args):
    if not 0 < args.pr <= 2**31 - 1 or not SHA.fullmatch(args.reviewed_head):
        return hold('A PR number and full reviewed SHA are required')
    if args.worker_verdict != 'clean':
        return hold('A clean worker verdict for the supplied head is required')
    if not args.feedback_clear:
        return hold('Outstanding actionable feedback must be cleared')
    if installation.permissions.get('contents') != 'write':
        return hold('App contents write permission is required')
    humans = set(args.human_logins.lower().split())
    awaiting = awaiting_approval(args)
    try:
        first, blocked = pull_snapshot(installation, args)
        if blocked:
            return blocked
        if not humans or not all(HUMAN_LOGIN.fullmatch(login) for login in humans):
            return awaiting
        author = first['author']['login'].lower()
        humans.discard(author)
        path = f'pulls/{args.pr}'
    except EVIDENCE_ERRORS:
        return hold('Could not verify pull request readiness')
    try:
        candidates, changes_requested = approvals(installation, path, humans, args.reviewed_head)
        if changes_requested:
            return hold('A submitted review still requests changes')
        reviewer = find_reviewer(installation, candidates)
        if reviewer is None:
            return awaiting
        current, blocked = pull_snapshot(installation, args)
        if blocked:
            return blocked
        if current['author']['login'].lower() != author or current['baseRefName'] != first['baseRefName']:
            return hold('PR author or base changed during verification')
        active, changes_requested = approvals(installation, path, humans, args.reviewed_head)
        if changes_requested:
            return hold('A submitted review still requests changes')
        if reviewer not in active:
            return awaiting
    except EVIDENCE_ERRORS:
        return awaiting

    try:
        merged = installation.api('PUT', path + '/merge', {
            'sha': args.reviewed_head, 'merge_method': args.method,
        })
        if merged['merged'] is True and SHA.fullmatch(merged['sha']):
            return dict(status='Merged', head=args.reviewed_head, merge_sha=merged['sha'])
    except EVIDENCE_ERRORS:
        pass
    return dict(hold('Merge was rejected or unconfirmed. Reconcile GitHub state before retrying.'),
                pr=args.pr, head=args.reviewed_head, outcome='unconfirmed')
