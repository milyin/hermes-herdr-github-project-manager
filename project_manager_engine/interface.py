"""One operator and tool API for the engine."""
import argparse
import json
import shlex

from .engine import COMMANDS, Engine
from .hermes_native import Native
from .settings import Hold


def outcome(home, command, args):
    try:
        return {'ok': True, 'result': Engine(home, Native(home)).execute(command, **args)}
    except Hold as exc:
        return {'ok': False, 'error': exc.as_dict()}
    except (TypeError, OSError):
        return {'ok': False, 'error': dict(code='local_error', message='Operation could not be verified')}


def bulk_properties(names):
    return {name: {'type': 'string'} for name in names.split()}


PANE = bulk_properties('pane_id')
FILTERS = {
    **bulk_properties('pane_id worker'),
    'role': {'type': 'string', 'enum': ['manager', 'worker', 'quota']},
    'state': {'type': 'string', 'enum': ['idle', 'starting', 'working', 'blocked', 'suspended', 'done', 'unknown']},
    'delivery': {'type': 'string', 'enum': ['none', 'unread', 'read', 'unknown']},
    'presence': {'type': 'string', 'enum': ['yes', 'no', 'unknown']},
    'lock': {'type': 'string', 'enum': ['locked', 'unlocked', 'unknown']},
}
TOOLS = {
    'run': ('Launch a named worker and return its pane and submitted turn.',
            ('name',), {**bulk_properties('name prompt working_directory'), 'parameters': {'type': 'object'}}),
    'steer': ('Submit an idle follow-up in the same worker pane.', ('pane_id', 'prompt'), {**PANE, **bulk_properties('prompt')}),
    'query': ('Read the verified result of a worker turn.', ('pane_id',), {**PANE, **bulk_properties('turn_id')}),
    'mark_read': ('Acknowledge handling the exact worker result or escalation.',
                  ('pane_id', 'turn_id'), {**PANE, **bulk_properties('turn_id attention_id')}),
    'set_comment': ('Set or clear the single-line worker pane comment.',
                    ('pane_id', 'comment'), {**PANE, **bulk_properties('comment')}),
    'close': ('Close an idle worker pane or mark it for deferred closure.', ('pane_id',), PANE),
    'workers': ('List configured workers and prompt parameter declarations.', (), {}),
    'panes': ('Inspect live manager and worker panes, optionally filtering the results.', (), FILTERS),
    'capacity': ('Check available worker slots and launch readiness. Supply a worker and its working directory '
                 'to preflight that launch.', (), bulk_properties('worker working_directory')),
    'quota': ('Inspect quota observations and holds for configured worker accounts.', (), bulk_properties('worker')),
}
class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Hold('invalid_arguments', message)


def parser():
    cli = Parser(prog='/project-manager', allow_abbrev=False, add_help=False)
    ops = cli.add_subparsers(dest='command')
    for command in COMMANDS:
        command_config = ops.add_parser(command.replace('_', '-'), allow_abbrev=False, add_help=False)
        if command == 'run':
            command_config.add_argument('name')
            command_config.add_argument('--cwd', dest='working_directory')
            command_config.add_argument('--param', action='append', default=[])
        if command in ('steer', 'query', 'mark_read', 'set_comment', 'close'):
            command_config.add_argument('pane_id')
        if command == 'mark_read':
            command_config.add_argument('turn_id')
            command_config.add_argument('--attention', dest='attention_id')
        if command == 'query':
            command_config.add_argument('--turn', dest='turn_id')
        if command in ('set_comment', 'steer'):
            command_config.add_argument('comment' if command == 'set_comment' else 'prompt')
        if command == 'panes':
            for name, schema in FILTERS.items():
                option = {'presence': 'present', 'pane_id': 'pane'}.get(name, name)
                command_config.add_argument('--' + option, dest=name, choices=schema.get('enum'))
        if command in ('quota', 'capacity'):
            command_config.add_argument('--worker')
        if command == 'capacity':
            command_config.add_argument('--cwd', dest='working_directory')
    return cli


def parse(text):
    head, _, rest = text.strip().partition(' ')
    if head == 'run':
        name, _, tail = rest.strip().partition(' ')
        tail = tail.lstrip()
        if name and not tail.startswith('--'):
            return 'run', dict(name=name, prompt=tail)
        # A standalone delimiter outside quoted option values ends structured arguments.
        offset = delimiter_position(rest)
        if offset is not None:
            prompt = rest[offset + 2:].lstrip()
            tokens = shlex.split(rest[:offset])
        else:
            prompt = ''
            tokens = shlex.split(rest)
        values = vars(parser().parse_args(['run', *tokens]))
        parameters = {}
        for pair in values.pop('param'):
            key, separator, value = pair.partition('=')
            if not separator or not key or key in parameters:
                raise Hold('invalid_parameters', 'Expected one distinct KEY=VALUE per --param')
            parameters[key] = value
        values.update(prompt=prompt, parameters=parameters)
    elif head in ('steer', 'set-comment'):
        pane, _, literal = rest.lstrip().partition(' ')
        if not pane or not literal:
            raise Hold('invalid_arguments', f'{head} requires a pane ID and text')
        key = 'prompt' if head == 'steer' else 'comment'
        if key == 'comment' and literal.strip() in ('""', "''"):
            literal = ''
        return head.replace('-', '_'), dict(pane_id=pane, **{key: literal})
    else:
        values = vars(parser().parse_args(shlex.split(text)))
    command = values.pop('command')
    return command.replace('-', '_') if command else None, {key: value for key, value in values.items() if value is not None}


def delimiter_position(text):
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if quote:
            if quote == char:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif (text[index:index + 2] == '--' and (index == 0 or text[index - 1].isspace())
              and (index + 2 == len(text) or text[index + 2].isspace())):
            return index
        index += 1
    return None


def format_result(data):
    return json.dumps(data, ensure_ascii=False, indent=2)
