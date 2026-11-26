"""Install an App command that uses a fixed private configuration file."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__:
    from .github_app import Config, GitHubError, private_file
else:
    from github_app import Config, GitHubError, private_file


def install_proxy(config_path, bin_dir):
    config = Config.read(config_path)
    with private_file(config.key):
        pass
    workflow = Path(__file__).resolve().parent
    script = (
        '#!/usr/bin/env -S python3 -IB\n'
        'from pathlib import Path\n'
        'import sys\n\n'
        f'sys.path.insert(0, {str(workflow)!r})\n'
        'from github_app import main\n\n'
        f'raise SystemExit(main(config_path=Path({str(config_path)!r})))\n'
    )
    bin_dir = bin_dir.absolute()
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    proxy = bin_dir / config.slug
    with TemporaryDirectory(prefix='.github-app-', dir=bin_dir) as temporary:
        entry = Path(temporary) / config.slug
        entry.write_text(script, encoding='utf-8')
        entry.chmod(0o700)
        try:
            proxy.hardlink_to(entry)
        except FileExistsError as exc:
            raise GitHubError(f'An App command already exists at {proxy}') from exc
    return proxy


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--bin-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(install_proxy(args.config, args.bin_dir))
    except GitHubError as exc:
        parser.exit(1, f'{exc}\n')
    except (KeyError, OSError, TypeError, ValueError):
        parser.exit(1, 'Could not read the private App files or install the proxy. Check both paths.\n')


if __name__ == '__main__':
    main()
