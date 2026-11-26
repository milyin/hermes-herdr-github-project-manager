"""Allocate and reclaim empty worker directories in explicitly configured roots."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
import uuid

from .settings import Hold, NAME, identifier, is_work_root


BINDING = ('pane_id', 'terminal_id', 'thread_id')
PROC = Path('/proc')


def identity(info):
    return [info.st_dev, info.st_ino]


def valid_identity(value, size):
    return isinstance(value, list) and len(value) == size and all(type(item) is int for item in value)


def write_record(path, record, *, replace=False):
    temporary = NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False)
    try:
        with temporary as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary.name, path)
        else:
            os.link(temporary.name, path)
    finally:
        Path(temporary.name).unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def open_root(root):
    """Walk from / with directory descriptors so no component can resolve a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in Path(root).parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Hold('invalid_work_root', 'Managed work root must be private and owned by the manager user')
        yield fd, identity(info)
    finally:
        os.close(fd)


def referenced(target):
    """Check for a surviving local process using the directory, including a background child."""
    # The root is private to this user. A process of that user we cannot inspect
    # makes liveness unknown. Identify it by its UIDs even if proc ownership differs.
    for pid in PROC.iterdir():
        if not pid.name.isdecimal():
            continue
        try:
            status = (pid / 'status').read_text()
            uids = next(line.split()[1:] for line in status.splitlines() if line.startswith('Uid:'))
            if str(os.getuid()) not in uids:
                continue
            for path in (pid / 'cwd', pid / 'root', *(pid / 'fd').iterdir()):
                try:
                    if identity(path.stat()) == target:
                        return True
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
        except (OSError, StopIteration, UnicodeError) as exc:
            raise Hold('process_state_unverified', 'Could not verify that local processes released the directory') from exc
    return False


class ManagedWork:
    def __init__(self, runtime):
        self.runtime = runtime
        self.profile = str(runtime.parent.resolve())
        self.records = {}
        for path in runtime.glob('work-*.json'):
            key = path.name[5:-5]
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),
                               encoding='utf-8') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                        continue
                    record = json.load(stream)
            except (OSError, ValueError):
                continue
            if self.valid_record(key, record) and record.get('profile') == self.profile:
                self.records[key] = record

    @staticmethod
    def valid_record(key, record):
        if not isinstance(record, dict):
            return False
        worker, _, suffix = key.rpartition('-')
        bound = record.get('binding')
        turn = record.get('turn_id')
        return (NAME.fullmatch(worker) and re.fullmatch(r'[a-f0-9]{32}', suffix) and record.get('worker') == worker
                and is_work_root(record.get('root')) and valid_identity(record.get('root_id'), 2)
                and valid_identity(record.get('directory_id'), 3)
                and all(identifier(record.get(field)) for field in ('instance', 'workspace_id'))
                and (bound is None or (isinstance(bound, dict) and all(identifier(bound.get(field)) for field in BINDING)))
                and (turn is None or (bound is not None and identifier(turn)))
                and type(record.get('closed')) is bool and (not record['closed'] or turn is not None))

    def path(self, key):
        return Path(self.records[key]['root']) / key

    def save(self, key, record):
        write_record(self.runtime / f'work-{key}.json', record, replace=key in self.records)
        self.records[key] = record

    def update(self, key, **fields):
        self.save(key, dict(self.records[key], **fields))

    def allocate(self, worker, instance, workspace_id):
        key = f'{worker.name}-{uuid.uuid4().hex}'
        try:
            with open_root(worker.work_root) as (fd, root_id):
                os.mkdir(key, mode=0o700, dir_fd=fd)
                info = os.stat(key, dir_fd=fd, follow_symlinks=False)
                os.fsync(fd)
            self.save(key, dict(profile=self.profile, worker=worker.name, instance=instance, workspace_id=workspace_id,
                                root=worker.work_root, root_id=root_id,
                                directory_id=[*identity(info), info.st_ctime_ns], closed=False))
        except OSError as exc:
            raise Hold('work_allocation_unverified', 'Could not allocate and record a managed directory') from exc
        return key

    def remove_empty(self, key):
        record = self.records[key]
        try:
            with open_root(record['root']) as (fd, root_id):
                if root_id != record['root_id']:
                    raise Hold('work_root_changed', 'The configured work root identity changed')
                self.check_directory(fd, key)
                if referenced(record['directory_id'][:2]):
                    raise Hold('directory_in_use', 'A local process is still using the directory')
                self.check_directory(fd, key)
                os.rmdir(key, dir_fd=fd)
                try:
                    os.stat(key, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise Hold('work_directory_changed', 'The directory name reappeared during cleanup')
        except OSError as exc:
            code = 'work_directory_not_empty' if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) else 'cleanup_unverified'
            raise Hold(code, 'Could not verify removal of the empty managed directory') from exc
        # If bookkeeping is interrupted, a missing pathname cannot authorize a
        # second cleanup attempt against any replacement directory.
        try:
            (self.runtime / f'work-{key}.json').unlink()
        except OSError:
            pass
        del self.records[key]

    def check_directory(self, fd, key):
        info = os.stat(key, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or [*identity(info), info.st_ctime_ns] != self.records[key]['directory_id']):
            raise Hold('work_directory_changed', 'The managed directory changed since allocation')
