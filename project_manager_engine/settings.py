"""Validated profile settings and single-pass prompt rendering."""
import re
import socket
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # Hermes installations ship ruamel.yaml.
    yaml = None


SKILL = 'hermes-herdr-github-project-manager'
VARIABLE = re.compile(r'{{([a-zA-Z_][a-zA-Z_0-9]*)}}')
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
WORKER_TAG = re.compile(r'[A-Za-z0-9]{1,4}')


class Hold(RuntimeError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return dict(code=self.code, message=str(self), **self.details)


def identifier(value):
    return isinstance(value, str) and bool(value.strip())


if yaml:
    class ProfileLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            result = {}
            for key, value in node.value:
                key = self.construct_object(key, deep=deep)
                if key in result:
                    raise ValueError(f'Duplicate setting {key!r}')
                result[key] = self.construct_object(value, deep=deep)
            return result


def read_yaml(source):
    if yaml:
        try:
            return yaml.load(source, Loader=ProfileLoader)
        except yaml.YAMLError as exc:
            raise ValueError('Invalid YAML in config.yaml') from exc
    from ruamel.yaml import YAML, YAMLError
    try:
        return YAML(typ='safe').load(source)
    except YAMLError as exc:
        raise ValueError('Invalid YAML in config.yaml') from exc


def config_file(home):
    try:
        with (home / 'config.yaml').open(encoding='utf-8') as source:
            return optional_mapping(read_yaml(source), 'profile')
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        raise Hold('invalid_config', f'Cannot read profile configuration: {exc}') from exc


def mapping(value, where):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise Hold('invalid_config', f'{where} must be a mapping with string keys')
    return value


def optional_mapping(value, where):
    return {} if value is None else mapping(value, where)


def require(test, message):
    if not test:
        raise Hold('invalid_config', message)


def text_field(record, key, where, default=None):
    value = record.get(key, default)
    require(isinstance(value, str) and bool(value.strip()) and '\0' not in value,
            f'{where}.{key} must be nonempty text')
    return value


def directory(value):
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_dir():
        raise Hold('invalid_directory', 'Working directory must be an existing absolute directory')
    return str(Path(value).resolve())


def is_work_root(value):
    if not isinstance(value, str) or '\0' in value:
        return False
    path = Path(value)
    return (path.is_absolute() and path.anchor == '/' and bool(path.name)
            and '..' not in path.parts and str(path) == value)


@dataclass(frozen=True)
class Prompt:
    path: Path
    text: str
    defaults: dict
    keys: frozenset

    @classmethod
    def read(cls, home, where, file, defaults, *, manager):
        path = home / file
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            raise Hold('invalid_config', f'{where} cannot read prompt file {path}') from exc
        require(bool(text.strip()) and '\0' not in text, f'{where} prompt is empty or invalid: {path}')
        defaults = mapping(defaults, f'{where}.parameters')
        for key, value in defaults.items():
            require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                    and (type(value) in (str, int, float, bool) or (value is None and not manager)),
                    f'{where} prompt {path} has an invalid declaration for {key}')
        keys = frozenset(VARIABLE.findall(text))
        leftover = VARIABLE.sub('', text)
        require('{{' not in leftover and '}}' not in leftover, f'{where} prompt {path} has a malformed reference')
        require(keys <= defaults.keys(), f'{where} prompt {path} uses undeclared parameters: {", ".join(sorted(keys - defaults.keys()))}')
        return cls(path, text, defaults, keys)

    def render(self, overrides=None):
        overrides = {} if overrides is None else overrides
        if not isinstance(overrides, dict) or not all(isinstance(key, str) for key in overrides):
            raise Hold('invalid_parameters', f'Named parameters for {self.path} must be a mapping')
        for key, value in overrides.items():
            if key not in self.keys or type(value) not in (str, int, float, bool):
                raise Hold('invalid_parameters', f'Invalid or unused parameter {key} for {self.path}')
        values = self.defaults | overrides
        for key, value in values.items():
            if value is None:
                raise Hold('missing_parameter', f'{self.path} requires {key}', parameter=key)
        return VARIABLE.sub(lambda match: str(values[match[1]]), self.text), values


@dataclass(frozen=True)
class Agent:
    name: str
    backend: str
    model: str
    cwd: str | None
    description: str
    prompt: Prompt
    tag: str | None = None
    launch_profile: str | None = None
    work_root: str | None = None
    reasoning_effort: str | None = None

    @classmethod
    def read(cls, home, record, where, *, manager=False):
        record = mapping(record, where)
        default_name = re.sub(r'[^a-z0-9-]', '-', socket.gethostname().lower()).strip('-')[:63] or 'manager'
        name = text_field(record, 'name', where, default_name if manager else None)
        require(NAME.fullmatch(name), f'{where}.name uses lowercase letters, digits and hyphens')
        backend = 'hermes' if manager else text_field(record, 'agent', where)
        effort = None
        if 'reasoning_effort' in record:
            require(backend == 'codex', f'{where}.reasoning_effort is supported only for Codex workers')
            effort = text_field(record, 'reasoning_effort', where)
            require(not any(char.isspace() for char in effort), f'{where}.reasoning_effort must be a single value')
        tag = None if manager else text_field(record, 'pane_tag', where)
        require(manager or WORKER_TAG.fullmatch(tag), f'{where}.pane_tag must use one to four ASCII letters or digits')
        launch = text_field(record, 'launch_profile', where) if 'launch_profile' in record else None
        work_root = None
        cwd = None
        if 'managed_work_root' in record:
            work_root = record['managed_work_root']
            require(not manager and 'working_directory' not in record and is_work_root(work_root),
                    f'{where}.managed_work_root must be an absolute worker root in place of working_directory')
        else:
            cwd = directory(text_field(record, 'working_directory', where))
        prompt = Prompt.read(home, f'{where} ({name})', text_field(record, 'prompt_file', where),
                             record.get('parameters', {}), manager=manager)
        return cls(name, backend, text_field(record, 'model', where), cwd,
                   text_field(record, 'description', where), prompt, tag, launch, work_root, effort)


@dataclass(frozen=True)
class Settings:
    instance: str
    limit: int
    manager: Agent
    workers: dict

    @classmethod
    def read(cls, home, raw):
        herdr = mapping(raw.get('herdr', {}), 'herdr')
        instance = text_field(herdr, 'instance', 'herdr', 'default')
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', instance), 'herdr.instance is invalid')
        limit = raw.get('max_working_panes')
        require(type(limit) is int and limit > 0, 'max_working_panes must be a positive integer')
        manager = Agent.read(home, raw.get('manager'), 'manager', manager=True)
        manager.prompt.render()
        records = raw.get('workers')
        require(isinstance(records, list), 'workers must be a list')
        workers = {}
        tags = set()
        for index, record in enumerate(records):
            worker = Agent.read(home, record, f'workers[{index}]')
            require(worker.name not in workers, f'Duplicate worker name: {worker.name}')
            require(worker.tag not in tags, f'Duplicate pane tag: {worker.tag}')
            workers[worker.name] = worker
            tags.add(worker.tag)
        return cls(instance, limit, manager, workers)


def raw_settings(home):
    profile = config_file(home)
    skills = optional_mapping(profile.get('skills'), 'skills')
    config = optional_mapping(skills.get('config'), 'skills.config')
    raw = optional_mapping(config.get(SKILL), SKILL)
    if 'enabled' in raw:
        require(type(raw['enabled']) is bool, 'enabled must be true or false')
    return raw
