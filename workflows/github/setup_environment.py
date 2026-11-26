"""Verify the installed App launcher before a managed GitHub workflow turn."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from github_app import REPOSITORY, SLUG


def prepare(request):
    repository = request['parameters']['REPO']
    slug = request['parameters']['github_identity']
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or not isinstance(slug, str) or not SLUG.fullmatch(slug)):
        raise ValueError('Configure the repository and App slug for this workflow')
    path = os.pathsep.join(entry for entry in os.get_exec_path() if Path(entry).is_absolute())
    launcher = shutil.which(slug, path=path)
    if not launcher:
        raise ValueError(f'Install the bundled GitHub launcher as {slug} on PATH')
    # Retain the App slug in argv[0] even when the installed command is a symlink.
    command = [launcher, '--repo', repository]
    help_result = subprocess.run([*command, 'merge-reviewed', '--help'],
                                 capture_output=True, text=True, timeout=8)
    if help_result.returncode or '--reviewed-head' not in help_result.stdout:
        raise ValueError(f'{slug} lacks merge-reviewed support. Install the bundled GitHub launcher')
    status = subprocess.run([*command, 'status'], capture_output=True, text=True, timeout=20)
    try:
        info = json.loads(status.stdout) if status.returncode == 0 else None
    except ValueError:
        info = None
    if (not isinstance(info, dict) or info.get('authenticated') is not True
            or info.get('actor') != f'{slug}[bot]' or info.get('repository') != repository):
        raise ValueError(f'Could not verify {slug}[bot] for {repository}')
    permissions = info.get('permissions')
    if request['manager'] and (not isinstance(permissions, dict) or permissions.get('contents') != 'write'):
        raise ValueError(f'{slug}[bot] requires contents write permission for {repository}')
    return {'setup_launcher': shlex.quote(launcher)}


def main():
    try:
        setup = prepare(json.load(sys.stdin))
    except (KeyError, OSError, TypeError, subprocess.SubprocessError):
        print(json.dumps({'error': 'Could not run the GitHub App launcher setup'}))
        return 1
    except ValueError as exc:
        print(json.dumps({'error': str(exc)}))
        return 1
    print(json.dumps(setup))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
