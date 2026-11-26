"""Observe GitHub review evidence for the manager's merge decision."""

from datetime import datetime
import json
import re
import subprocess

from .settings import Hold


HEAD = re.compile(r'[0-9a-f]{40}')
LOGIN = re.compile(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*')
AWAITING_APPROVAL = 'Awaiting human approval'


def github_handoff(parameters, pr, reviewed_head):
    repository = parameters.get('REPO')
    identity = parameters.get('github_identity')
    if (not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository)
            or repository.split('/')[1] in {'.', '..'}
            or not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', identity)):
        raise Hold('invalid_config', 'GitHub handoff requires the manager repository and App slug')
    if type(pr) is not int or pr < 1 or not isinstance(reviewed_head, str) or not HEAD.fullmatch(reviewed_head):
        raise Hold('invalid_arguments', 'GitHub handoff requires a PR number and full reviewed head SHA')

    configured = parameters.get('human_reviewers', '')
    if not isinstance(configured, str):
        raise Hold('invalid_config', 'human_reviewers must be a comma-separated list of GitHub logins')
    reviewers = {login.strip().lower() for login in configured.split(',')} if configured.strip() else set()
    if not all(LOGIN.fullmatch(login) for login in reviewers):
        raise Hold('invalid_config', 'human_reviewers must be a comma-separated list of GitHub logins')

    def app(*args):
        output = subprocess.run([identity, '--repo', repository, *args], check=True, capture_output=True,
                                encoding='utf-8', timeout=30).stdout
        return json.loads(output)

    def pull():
        row = app('gh', 'pr', 'view', str(pr), '--repo', repository, '--json',
                  'author,headRefOid,isDraft,mergeable,mergeStateStatus,reviewDecision,state')
        if (not isinstance(row, dict) or not isinstance(row.get('headRefOid'), str)
                or not HEAD.fullmatch(row['headRefOid']) or type(row.get('isDraft')) is not bool
                or row.get('state') not in ('OPEN', 'CLOSED', 'MERGED')):
            raise ValueError('Incomplete pull request evidence')
        return row

    result = dict(section='Need attention', status=AWAITING_APPROVAL, reviewed_head=reviewed_head)

    def current(row):
        head = row['headRefOid']
        if row['state'] == 'MERGED':
            return dict(result, section='Done', status='Merged', head=head)
        if row['state'] == 'CLOSED':
            return dict(result, status='Pull request closed', head=head)
        if head != reviewed_head:
            return dict(result, status='Head changed', head=head)
        if row['isDraft']:
            return dict(result, status='Pull request is a draft', head=head)
        return None

    # This result describes current GitHub evidence. The manager submits the merge separately.
    try:
        status = app('status')
        if (not isinstance(status, dict) or status.get('authenticated') is not True
                or status.get('actor') != f'{identity}[bot]' or status.get('repository') != repository):
            raise ValueError('Unverified GitHub App identity')
        row = pull()
        if superseded := current(row):
            return superseded
        result['head'] = row['headRefOid']
        if not reviewers:
            return dict(result, reason='No human reviewer accounts configured')
        author = row.get('author')
        if not isinstance(author, dict) or not isinstance(author.get('login'), str) or not author['login']:
            raise ValueError('Unverified pull request author')

        endpoint = f'repos/{repository}/pulls/{pr}/reviews?per_page=100'
        pages = app('gh', 'api', endpoint, '--method', 'GET', '--paginate', '--slurp')
        approvals = eligible_reviews(pages, reviewers - {author['login'].lower()}, reviewed_head)
        for reviewer in approvals:
            grant = app('gh', 'api', f'repos/{repository}/collaborators/{reviewer["login"]}/permission',
                        '--method', 'GET')
            if not isinstance(grant, dict) or 'permission' not in grant:
                raise ValueError('Unverified collaborator permission')
            if grant['permission'] not in ('admin', 'write'):
                continue
            user = grant.get('user')
            if (not isinstance(user, dict) or user.get('type') != 'User'
                    or user.get('id') != reviewer['id'] or user.get('login') != reviewer['login']):
                raise ValueError('Unverified collaborator identity')
            latest = pull()
            if superseded := current(latest):
                return superseded
            if latest.get('author') != author:
                raise ValueError('Pull request author changed')
            ready = (latest.get('mergeable') == 'MERGEABLE' and latest.get('mergeStateStatus') == 'CLEAN'
                     and 'reviewDecision' in latest and latest['reviewDecision'] in (None, '', 'APPROVED'))
            return dict(result, status='Ready to merge' if ready else 'Awaiting GitHub requirements',
                        approver=reviewer['login'])
        return dict(result, reason='No eligible submitted approval for the reviewed head')
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(result, reason='Could not verify GitHub review evidence')


def eligible_reviews(pages, reviewers, reviewed_head):
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError('Incomplete review history')
    latest = {}
    seen = set()
    identities = {}
    # The REST reviews endpoint returns reviews in chronological order across pages.
    for page in pages:
        for review in page:
            if not isinstance(review, dict) or type(review.get('id')) is not int or review['id'] in seen:
                raise ValueError('Unverified review history')
            seen.add(review['id'])
            state = review.get('state')
            if state in ('COMMENTED', 'PENDING'):
                continue
            if state not in ('APPROVED', 'DISMISSED', 'CHANGES_REQUESTED'):
                raise ValueError('Unverified review state')
            user = review.get('user')
            login = user.get('login') if isinstance(user, dict) else None
            if not isinstance(login, str) or not login.strip() or type(user.get('id')) is not int:
                raise ValueError('Unverified reviewer identity in review history')
            login = login.lower()
            if identities.setdefault(user['id'], login) != login:
                raise ValueError('Conflicting reviewer identity in review history')
            if login in reviewers:
                latest[login] = review
    approvals = []
    for review in latest.values():
        user = review['user']
        if (review['state'] != 'APPROVED' or review.get('commit_id') != reviewed_head
                or user.get('type') != 'User' or type(user.get('id')) is not int):
            continue
        submitted = review.get('submitted_at')
        if not isinstance(submitted, str) or datetime.fromisoformat(submitted.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('Unverified review submission time')
        approvals.append(user)
    return approvals
