"""Fixed GitHub operations executed by the host for the manager."""

import json
import os
import re
import subprocess

from .settings import Hold


HEAD = re.compile(r'[0-9a-f]{40}')
PULL_FIELDS = 'author,headRefOid,isDraft,mergeable,mergeStateStatus,reviewDecision,state'


class GitHubApp:
    def __init__(self, parameters):
        self.repository = parameters.get('REPO')
        self.identity = parameters.get('github_identity')
        if (not isinstance(self.repository, str)
                or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', self.repository)
                or self.repository.split('/')[1] in {'.', '..'}
                or not isinstance(self.identity, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', self.identity)):
            raise Hold('invalid_config', 'GitHub operations require a configured repository and App slug')

    def _run(self, *args, data=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        return subprocess.run([self.identity, '--repo', self.repository, *args], check=True,
                              capture_output=True, env=env, input=data, timeout=30).stdout

    def _json(self, *args):
        return json.loads(self._run(*args))

    def verify(self, *, wiki_write=False):
        status = self._json('status')
        if (not isinstance(status, dict) or status.get('authenticated') is not True
                or status.get('actor') != f'{self.identity}[bot]' or status.get('repository') != self.repository):
            raise ValueError('Unverified GitHub App identity')
        if wiki_write:
            permissions = status.get('permissions')
            if not isinstance(permissions, dict) or permissions.get('contents') != 'write':
                raise ValueError('The GitHub App requires Contents write access to publish the wiki')

    def pull(self, pr, *, discussion=False):
        if type(pr) is not int or pr < 1:
            raise Hold('invalid_arguments', 'Supply a positive pull request number')
        fields = PULL_FIELDS
        if discussion:
            fields += ',number,url,baseRefName,headRefName,comments,reviews,autoMergeRequest'
        row = self._json('gh', 'pr', 'view', str(pr), '--repo', self.repository, '--json', fields)
        if (not isinstance(row, dict) or not isinstance(row.get('headRefOid'), str)
                or not HEAD.fullmatch(row['headRefOid']) or type(row.get('isDraft')) is not bool
                or row.get('state') not in ('OPEN', 'CLOSED', 'MERGED')):
            raise ValueError('Incomplete pull request evidence')
        return row

    def issues(self, label):
        if not isinstance(label, str) or not label.strip():
            raise Hold('invalid_config', 'GitHub issue discovery requires a configured LABEL')
        rows = self._json('gh', 'issue', 'list', '--repo', self.repository, '--state', 'open',
                          f'--label={label}', '--limit', '100', '--json', 'number,title,url')
        if not isinstance(rows, list):
            raise ValueError('Incomplete issue listing')
        return {'issues': rows}

    def reviews(self, pr):
        endpoint = f'repos/{self.repository}/pulls/{pr}/reviews?per_page=100'
        return self._json('gh', 'api', endpoint, '--method', 'GET', '--paginate', '--slurp')

    def collaborator(self, login):
        return self._json('gh', 'api', f'repos/{self.repository}/collaborators/{login}/permission', '--method', 'GET')
