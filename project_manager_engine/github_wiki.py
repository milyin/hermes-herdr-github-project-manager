"""Compare-and-swap a single dashboard page in the configured GitHub wiki."""

import re
from tempfile import TemporaryDirectory

from .github_app import HEAD
from .settings import Hold


WIKI_BRANCH = re.compile(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*')
WIKI_PAGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\.md')


def wiki_dashboard(app, page, *, revision=None, markdown=None):
    if not isinstance(page, str) or not WIKI_PAGE.fullmatch(page):
        raise Hold('invalid_config', 'wiki_page must name a root-level Markdown page')
    publish = markdown is not None
    if publish and (not isinstance(markdown, str) or len(markdown) > 100_000 or '\0' in markdown
                    or not isinstance(revision, str) or not HEAD.fullmatch(revision)):
        raise Hold('invalid_arguments', 'Supply a wiki revision and at most 100,000 characters of Markdown')
    app.verify(wiki_write=publish)
    remote = f'https://github.com/{app.repository}.wiki.git'
    with TemporaryDirectory(prefix='project-manager-wiki-') as work:
        def git(*args, data=None):
            return app._run('git', '-C', work, *args, data=data)

        git('init', '--bare', '--template=')
        lines = git('ls-remote', '--symref', remote, 'HEAD').decode('ascii').splitlines()
        if len(lines) != 2:
            raise ValueError('Unverified wiki HEAD')
        target = re.fullmatch(r'ref: (.+)\tHEAD', lines[0])
        tip = re.fullmatch(r'([0-9a-f]{40})\tHEAD', lines[1])
        if not target or not WIKI_BRANCH.fullmatch(target[1]) or not tip:
            raise ValueError('Unverified wiki HEAD')
        branch, head = target[1], tip[1]
        git('check-ref-format', branch)
        if publish and revision != head:
            raise Hold('wiki_changed', 'The wiki changed. Read its dashboard again before updating it.')
        git('fetch', '--no-tags', '--no-recurse-submodules', '--depth=1', remote, branch)
        if git('rev-parse', '--verify', 'FETCH_HEAD^{commit}').decode('ascii').strip() != head:
            raise Hold('wiki_changed', 'The wiki changed during the read. Read its dashboard again.')

        entry = git('ls-tree', head, '--', page)
        if entry and not re.fullmatch(rb'100644 blob [0-9a-f]{40}\t' + re.escape(page.encode('ascii')) + rb'\n', entry):
            raise ValueError('The dashboard must be a regular Markdown file')
        previous = git('show', f'{head}:{page}') if entry else b''
        if not publish:
            return {'revision': head, 'markdown': previous.decode('utf-8')}

        content = markdown.encode('utf-8')
        if previous == content:
            return {'revision': head, 'updated': False}
        blob = git('hash-object', '-w', '--stdin', data=content).decode('ascii').strip()
        if not HEAD.fullmatch(blob):
            raise ValueError('Unverified dashboard blob')
        git('read-tree', head)
        git('update-index', '--add', '--cacheinfo', f'100644,{blob},{page}')
        tree = git('write-tree').decode('ascii').strip()
        if not HEAD.fullmatch(tree):
            raise ValueError('Unverified wiki tree')
        commit = git('commit-tree', tree, '-p', head, '-m', 'Update project manager dashboard').decode('ascii').strip()
        if not HEAD.fullmatch(commit):
            raise ValueError('Unverified wiki commit')
        git('push', '--recurse-submodules=no', f'--force-with-lease={branch}:{head}', remote, f'{commit}:{branch}')
        return {'revision': commit, 'updated': True}
