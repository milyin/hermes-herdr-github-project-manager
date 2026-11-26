"""Create the selected repository's opt-in label through its GitHub App."""

import argparse
import json
import re
import subprocess


def ensure_label(repository, identity, label):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', identity):
        raise ValueError('The GitHub App identity must be its slug')
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('The repository must be OWNER/REPO')
    if not label.strip():
        raise ValueError('The opt-in label must be nonempty')

    def app(*args):
        result = subprocess.run([identity, '--repo', repository, *args], check=True,
                                stdout=subprocess.PIPE, text=True, timeout=90)
        return result.stdout

    status = json.loads(app('status'))
    if (not isinstance(status, dict) or status.get('authenticated') is not True
            or status.get('actor') != f'{identity}[bot]' or status.get('repository') != repository):
        raise ValueError(f'Cannot verify {identity}[bot] on {repository}')
    permissions = status.get('permissions')
    if not isinstance(permissions, dict) or permissions.get('contents') != 'write':
        raise ValueError(f'Setup requires permissions.contents: write for {identity}[bot] on {repository}')

    endpoint = f'repos/{repository}/labels'

    def find_label():
        lines = app('gh', 'api', endpoint, '--paginate', '--jq', '.[] | .name | @json').splitlines()
        names = [json.loads(line) for line in lines]
        if not all(isinstance(name, str) for name in names):
            raise ValueError(f'Cannot verify the labels on {repository}')
        return next((name for name in names if name.casefold() == label.casefold()), None)

    existing = find_label()
    if existing is not None:
        return existing
    try:
        app('gh', 'api', endpoint, '--method', 'POST', '--raw-field', f'name={label}',
            '--raw-field', 'color=1d76db', '--raw-field', 'description=Opt in to project-manager automation')
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # A concurrent setup or a lost response may have left the label in place.
        existing = find_label()
        if existing is None:
            raise
        return existing
    return label


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('identity')
    parser.add_argument('label')
    args = parser.parse_args()
    try:
        print(ensure_label(args.repository, args.identity, args.label))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Opt-in label setup failed for {args.repository}: {exc}\n')


if __name__ == '__main__':
    main()
