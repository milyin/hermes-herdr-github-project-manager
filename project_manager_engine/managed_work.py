"""Allocate worker directories and retain their private ownership records."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
import uuid

from .settings import Hold, NAME, identifier, is_work_root


BINDING = ('pane_id', 'terminal_id', 'thread_id')


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
                and (turn is None or (bound is not None and identifier(turn))))

    def path(self, key):
        return Path(self.records[key]['root']) / key

    def retention(self, key):
        # A layout, ordinary close receipt, or legacy "closed" flag cannot
        # establish exclusive access to the recorded directory.
        return dict(path=str(self.path(key)), status='retained', reason='cleanup_guard_unavailable')

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
                                directory_id=[*identity(info), info.st_ctime_ns]))
        except OSError as exc:
            raise Hold('work_allocation_unverified', 'Could not allocate and record a managed directory') from exc
        return key
