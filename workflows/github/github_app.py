#!/usr/bin/env python3
"""Run the GitHub workflow as an operator-configured App installation."""

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .merge_reviewed import add_arguments, merge_reviewed
else:
    from merge_reviewed import add_arguments, merge_reviewed


API = 'https://api.github.com'
SLUG = re.compile(r'[a-z0-9][a-z0-9-]*')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9_.-]+')
GIT_LOCAL = {
    'add', 'branch', 'checkout', 'cherry-pick', 'commit', 'diff', 'for-each-ref', 'log',
    'ls-files', 'merge', 'merge-base', 'rebase', 'remote', 'reset', 'restore', 'rev-parse',
    'show', 'status', 'switch', 'worktree',
}
GIT_REMOTE = {
    'clone': {'--bare', '--no-checkout', '--no-tags', '--quiet', '--single-branch'},
    'fetch': {'--force', '--no-tags', '--prune', '--quiet'},
    'ls-remote': {'--exit-code', '--heads', '--refs', '--tags'},
    'pull': {'--ff-only', '--no-rebase', '--no-tags', '--quiet'},
    'push': {'--dry-run', '--force-with-lease', '--porcelain', '--quiet'},
}
GH_COMMANDS = {
    'issue': {'comment', 'list', 'view'},
    'pr': {'checks', 'comment', 'create', 'diff', 'edit', 'list', 'ready', 'view'},
}


class GitHubError(ValueError):
    """An App request failed without exposing credentials or response bodies."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def request(method, path, token, data=None):
    headers = {
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {token}',
        'User-Agent': 'herdr-project-manager',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    payload = None if data is None else json.dumps(data).encode()
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    message = Request(API + path, data=payload, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(message, timeout=30) as response:
            return json.load(response), response.headers.get('Link', '')
    except HTTPError as exc:
        raise GitHubError(f'GitHub {method} failed (HTTP {exc.code})') from None


@contextmanager
def private_file(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise GitHubError('App configuration and key must be private files owned by the operator')
        yield source


@dataclass(frozen=True)
class Config:
    slug: str
    app_id: str
    repository: str
    key: Path

    @classmethod
    def read(cls, path, repository=None, launcher_name=None):
        if not path.is_absolute():
            raise GitHubError('Configure an absolute path to the App configuration')
        with private_file(path) as source:
            settings = json.load(source)
        slug = settings['app_slug']
        app_id = str(settings['app_id'])
        if not SLUG.fullmatch(slug) or not re.fullmatch(r'[1-9][0-9]*', app_id):
            raise GitHubError('Configure a valid GitHub App slug and numeric App ID')
        if launcher_name is not None and SLUG.fullmatch(launcher_name) and launcher_name != slug:
            raise GitHubError('The installed launcher name and configured App slug differ')
        configured_repo = settings['repository']
        if not REPOSITORY.fullmatch(configured_repo) or configured_repo.split('/')[1] in {'.', '..'}:
            raise GitHubError('Configure a valid App repository')
        if repository is not None and repository != configured_repo:
            raise GitHubError('The target repository differs from the configured App scope')
        key = Path(settings['private_key'])
        if not key.is_absolute():
            raise GitHubError('Configure an absolute path to the App private key')
        return cls(slug, app_id, configured_repo, key)


def jwt(config):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=')

    now = int(time.time())
    claims = json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': config.app_id}).encode()
    message = encode(b'{"alg":"RS256","typ":"JWT"}') + b'.' + encode(claims)
    with private_file(config.key) as key:
        fd = key.fileno()
        signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', f'/proc/self/fd/{fd}'],
                                input=message, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=(fd,), timeout=10, check=True).stdout
    return (message + b'.' + encode(signed)).decode('ascii')


@dataclass(frozen=True)
class Installation:
    repository: str
    actor: str
    bot_id: int
    token: str = field(repr=False)
    permissions: dict
    expires_at: str

    def status(self):
        return dict(authenticated=True, actor=self.actor, repository=self.repository,
                    permissions=self.permissions, expires_at=self.expires_at)

    def api(self, method, suffix, data=None):
        return request(method, f'/repos/{self.repository}/{suffix}', self.token, data)[0]

    def pages(self, suffix):
        path = f'/repos/{self.repository}/{suffix}'
        page = 1
        while True:
            data, links = request('GET', f'{path}?per_page=100&page={page}', self.token)
            if not isinstance(data, list):
                raise GitHubError('GitHub returned an invalid list')
            has_next = re.search(r';\s*rel="next"', links) is not None
            if not data and (page > 1 or has_next):
                raise GitHubError('GitHub returned an incomplete list')
            yield from data
            if not has_next:
                return
            page += 1

    def graphql(self, query, variables):
        body, _ = request('POST', '/graphql', self.token, dict(query=query, variables=variables))
        if body.get('errors'):
            raise GitHubError('GitHub could not verify the pull request')
        return body['data']


def authenticate(config):
    proof = jwt(config)
    app, _ = request('GET', '/app', proof)
    if str(app['id']) != config.app_id or app['slug'] != config.slug:
        raise GitHubError('GitHub returned a different App identity')
    owner, name = config.repository.split('/')
    installation, _ = request('GET', f'/repos/{config.repository}/installation', proof)
    identifier = installation['id']
    if (type(identifier) is not int or identifier < 1 or str(installation['app_id']) != config.app_id
            or installation['account']['login'].casefold() != owner.casefold()):
        raise GitHubError('GitHub returned a different App installation')
    access, _ = request('POST', f'/app/installations/{identifier}/access_tokens', proof, {'repositories': [name]})
    token, permissions = access['token'], access['permissions']
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', token)
            or not isinstance(permissions, dict)):
        raise GitHubError('GitHub returned invalid App credentials')
    repository, _ = request('GET', f'/repos/{config.repository}', token)
    if repository['full_name'].casefold() != config.repository.casefold():
        raise GitHubError('GitHub returned a different repository')
    actor = config.slug + '[bot]'
    bot, _ = request('GET', '/users/' + quote(actor, safe=''), token)
    if bot['login'] != actor or bot['type'] != 'Bot' or type(bot['id']) is not int or bot['id'] < 1:
        raise GitHubError('GitHub returned a different App bot')
    return Installation(config.repository, actor, bot['id'], token, permissions, access['expires_at'])


def cli_environment(home):
    env = {key: value for key, value in os.environ.items()
           if key in {'PATH', 'TERM', 'LANG', 'LANGUAGE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
           or key.startswith('LC_') or key.lower() in {'http_proxy', 'https_proxy', 'no_proxy'}}
    env.update(HOME=home, XDG_CONFIG_HOME=home, XDG_DATA_HOME=home, XDG_CACHE_HOME=home,
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/usr/bin/false', GIT_ALLOW_PROTOCOL='',
               GIT_EDITOR='/usr/bin/false', GIT_PAGER='cat', GH_EDITOR='/usr/bin/false', GH_PAGER='cat',
               GH_PROMPT_DISABLED='1', GH_HOST='github.com', GH_CONFIG_DIR=home,
               GH_NO_UPDATE_NOTIFIER='1', GH_NO_EXTENSION_UPDATE_NOTIFIER='1')
    return env


def github_command(repository, args):
    if any(arg in {'--', '--editor', '--web'} or arg.startswith(('--host', '--header', '--repo', '-R'))
           for arg in args):
        raise GitHubError('The launcher controls the repository, host and execution environment')
    if args[:1] == ['api'] and len(args) > 1:
        endpoint = urlsplit(args[1])
        path = unquote(endpoint.path).removeprefix('/')
        base = f'repos/{repository}'
        if (endpoint.scheme or endpoint.netloc or endpoint.fragment
                or (path != base and not path.startswith(base + '/'))
                or not re.fullmatch(r'[A-Za-z0-9_.@/-]+', path)
                or any(part in ('', '.', '..') for part in path.split('/'))
                or re.fullmatch(re.escape(base) + r'/pulls/[0-9]+/merge', path, re.IGNORECASE)):
            raise GitHubError('Use a repository API endpoint or the checked merge-reviewed command')
        if any(arg.startswith('-H') for arg in args[2:]):
            raise GitHubError('The launcher controls the request headers')
        return ['gh', *args]
    if len(args) < 2 or args[1] not in GH_COMMANDS.get(args[0], ()):
        raise GitHubError('Use a supported workflow gh command')
    if args[1] not in ('create', 'list') and (len(args) < 3 or not re.fullmatch(r'[1-9][0-9]*', args[2])):
        raise GitHubError('Pass a numeric issue or pull request number after the command')
    if args[:2] == ['pr', 'create'] and not any(arg == '--head' or arg.startswith('--head=') for arg in args[2:]):
        raise GitHubError('Push through scoped Git first and specify --head when creating a PR')
    return ['gh', *args, '--repo', repository]


def git_command(installation, args, env):
    command, *options = args or ['']
    if command not in GIT_LOCAL and command not in GIT_REMOTE:
        raise GitHubError('Use a supported workflow git command')
    name, bot_id = installation.actor, installation.bot_id
    email = f'{bot_id}+{name}@users.noreply.github.com'
    env.update(GIT_AUTHOR_NAME=name, GIT_COMMITTER_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
    git = ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'credential.helper=']
    if command in GIT_LOCAL:
        return [*git, command, *options]

    position = next((index for index, value in enumerate(options) if not value.startswith('-')), len(options))
    if position == len(options) or any(value not in GIT_REMOTE[command] for value in options[:position]):
        raise GitHubError('Use supported remote Git options followed by a repository HTTPS URL')
    remote = options[position]
    expected = f'https://github.com/{installation.repository}'
    if (remote not in {expected + '.git', expected + '.wiki.git'}
            or any(value.startswith('-') for value in options[position + 1:])):
        raise GitHubError('Remote Git requires the exact credential-free HTTPS repository or wiki URL')
    if command == 'push' and (
            len(options) != position + 2
            or not re.fullmatch(r'[^:]+:refs/heads/[^:*]+', options[-1])):
        raise GitHubError('Push one explicit local ref to a named remote branch')
    check_git_config(env)
    if command == 'clone':
        options = ['--template=', *options]
    credential = base64.b64encode(f'x-access-token:{installation.token}'.encode()).decode()
    # The header is limited to the verified URL, absent from argv and never used for local Git.
    env.update(GIT_ALLOW_PROTOCOL='https', GIT_CONFIG_COUNT='1',
               GIT_CONFIG_KEY_0=f'http.{remote}.extraHeader', GIT_CONFIG_VALUE_0='Authorization: Basic ' + credential)
    return [*git, '-c', 'http.followRedirects=false', '-c', 'http.sslVerify=true',
            '-c', 'submodule.recurse=false', '-c', 'fetch.recurseSubmodules=false',
            '-c', 'push.recurseSubmodules=no', '-c', 'pull.ff=only', command, *options]


def check_git_config(env):
    output = subprocess.check_output(['git', 'config', '--no-includes', '--null', '--name-only', '--list'],
                                     env=env, timeout=10).decode()
    unsafe = ('alias.', 'credential.', 'diff.', 'filter.', 'gpg.', 'http.', 'include.', 'includeif.',
              'merge.', 'protocol.', 'url.')
    for key in output.split('\0'):
        if (key.startswith(unsafe)
                or key in {'core.fsmonitor', 'core.gitproxy', 'core.sshcommand', 'push.gpgsign'}
                or re.fullmatch(r'remote\..+\.(vcs|promisor|proxy|receivepack|uploadpack)', key)):
            raise GitHubError('Git configuration contains a transport or credential override')


def main(argv=None, *, config_path=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--repo', required=True)
    if config_path is None:
        parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status')
    add_arguments(commands.add_parser('merge-reviewed'))
    for name in ('gh', 'git'):
        commands.add_parser(name).add_argument('native_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        config = Config.read(config_path if config_path is not None else args.config,
                             args.repo, Path(sys.argv[0]).name)
        installation = authenticate(config)
        if args.action == 'status':
            print(json.dumps(installation.status()))
            return 0
        if args.action == 'merge-reviewed':
            print(json.dumps(merge_reviewed(installation, args)))
            return 0
        with TemporaryDirectory(prefix='project-manager-app-') as home:
            env = cli_environment(home)
            if args.action == 'gh':
                command = github_command(installation.repository, args.native_args)
                env.update(GH_TOKEN=installation.token, GH_REPO=installation.repository)
            else:
                command = git_command(installation, args.native_args, env)
            return subprocess.run(command, env=env).returncode
    except GitHubError as exc:
        print(f'GitHub App: {exc}', file=sys.stderr)
    except (AttributeError, KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        print('Could not verify or run the scoped GitHub App operation. Check workflow setup.',
              file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
