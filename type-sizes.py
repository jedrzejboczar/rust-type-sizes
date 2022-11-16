#!/usr/bin/env python

import re
import os
import sys
import toml
import pprint
import shutil
import logging
import argparse
import datetime
import subprocess
import dataclasses
from pathlib import Path
from typing import Union, Optional

import jinja2


log = logging.getLogger('type_sizes')
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')


def touch(path):
    path = Path(path)
    assert path.is_file(), f'Refusing to touch non-existing file: "{path}"'
    path.touch()


def compile(args):
    cmd = ['cargo', '+nightly', 'rustc', *args, '--', '-Zprint-type-sizes']
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True)
    return proc, cmd


def g(name, pattern):
    return r'(?P<{}>{})'.format(name, pattern)


def regex(fmt, **groups):
    kwargs = {
        name: g(name, pattern)
        for (name, pattern) in groups.items()
    }
    pattern = fmt.format(**kwargs)
    return re.compile(pattern)

INDENT = 4

PATTERNS = {
    'main': regex(r'^print-type-size {tail}$', tail=r'.*'),
    'type': regex(r'^type: `{name}`: {size} bytes, alignment: {align} bytes$',
                  name=r'[^`]+', size=r'\d+', align=r'\d+'),
    'inner': regex(r'^{indent}{type}( `{name}`)?: {size} bytes(, offset: {offset} bytes)?(, alignment: {align} bytes)?$',
                   indent=r'\s+', type=r'[a-z ]+', name=r'[^`]+', size=r'\d+', offset=r'\d+', align=r'\d+'),
    'name_sep': regex(r'::|<|>|->'),
}


class NameParseMixin:
    def name_split(self) -> list[tuple[str, int]]:
        tokens = []
        i = 0
        brackets_level = 0

        for match in PATTERNS['name_sep'].finditer(self.name):
            start, end = match.span()

            if start > i:
                tokens.append((self.name[i:start], brackets_level))

            if match.group() == '<':
                brackets_level += 1

            tokens.append((self.name[start:end], brackets_level))

            if match.group() == '>':
                brackets_level -= 1
                if brackets_level < 0:
                    log.warning('Negative angle brackets_level=%s for name=%s', brackets_level, self.name)

            i = end

        if i < len(self.name):
            tokens.append((self.name[i:], brackets_level))

        return tokens


@dataclasses.dataclass()
class Discriminant:
    size: int


@dataclasses.dataclass()
class Padding:
    size: int


@dataclasses.dataclass()
class EndPadding:
    size: int


@dataclasses.dataclass()
class Field(NameParseMixin):
    name: str
    size: int
    offset: Optional[int]
    alignment: Optional[int]


@dataclasses.dataclass()
class Variant(NameParseMixin):
    name: str
    size: int
    tree: list[Union[Padding, Field]]


@dataclasses.dataclass()
class Type(NameParseMixin):
    name: str
    size: int
    alignment: int
    tree: list[Union[Discriminant, Padding, EndPadding, Variant, Field]]


def parse(lines) -> list[Type]:
    # for name, pattern in PATTERNS.items():
    #     print(f'PATTERN {name}:')
    #     print(pattern.pattern)
    # print()

    # filter out non-related lines
    lines = map(PATTERNS['main'].match, lines)
    lines = filter(None, lines)
    lines = map(lambda m: m.group('tail'), lines)

    lines = list(lines)

    types = []
    while len(lines):
        lines, typ = parse_type(lines)
        if typ is not None:
            types.append(typ)

    return types


def parse_type(lines) -> tuple[list[str], Type]:
    line, *lines = lines

    match = PATTERNS['type'].match(line)
    if not match:
        log.error('Ignoring line (expected type): %s', line)
        return lines

    lines, tree = parse_tree(lines)

    return lines, Type(
        name=match.group('name'),
        size=int(match.group('size')),
        alignment=int(match.group('align')),
        tree=tree,
    )


def parse_tree(lines, depth=1) -> tuple[list[str], list[Union[Discriminant, Padding, Variant, Field]]]:
    tree = []

    while len(lines) > 0:
        # Iterate until we find "type" line again
        match = PATTERNS['inner'].match(lines[0])
        if not match:
            return lines, tree

        group = lambda name: match.group(name)
        igroup = lambda name: int(group(name))
        igroup_opt = lambda name: int(group(name)) if group(name) else None

        indent = len(group('indent'))

        # Test line indent
        if indent > depth * INDENT:  # Parse inner subtree
            lines, subtree = parse_tree(lines, depth=depth + 1)

            prev = tree[-1]
            assert isinstance(prev, Variant)
            prev.tree = subtree

            continue
        elif indent < depth * INDENT:  # Go up the tree
            return lines, tree

        # consume this line
        line, *lines = lines

        typ = group('type')
        result = None
        if typ == 'discriminant':
            result = Discriminant(size=group('size'))
        elif typ == 'padding':
            result = Padding(size=igroup('size'))
        elif typ == 'end padding':
            result = EndPadding(size=igroup('size'))
        elif typ == 'field':
            result = Field(
                name=group('name'),
                size=igroup('size'),
                offset=igroup_opt('offset'),
                alignment=igroup_opt('align'),
            )
        elif typ == 'variant':
            result = Variant(name=group('name'), size=igroup('size'), tree=None)

        assert result is not None, f'Parsing failed on: {line}'
        tree.append(result)

    return lines, tree


def walk_tree(tree, callback):
    callback(tree)
    for subtree in getattr(tree, 'tree', []) or []:
        walk_tree(subtree, callback)


def trim_name(node, max_length):
    if hasattr(node, 'name') and len(node.name) > max_length:
        # add ellipsis
        trimmed = node.name[:max_length] + '…'

        # add missing closing brackets
        left, right = 0, 0
        for match in PATTERNS['name_sep'].finditer(trimmed):
            if match.group() == '<':
                left += 1
            elif match.group() == '>':
                right += 1
        if right < left:
            trimmed += '>' * (left - right)

        node.name = trimmed


def fs_walk_up(dir=None):
    if dir is None:
        dir = os.getcwd()
    while True:
        yield dir
        dir, tail = os.path.split()
        if not tail:
            break


def parse_cargo_toml(dir):
    try:
        with open(os.path.join(dir, 'Cargo.toml')) as f:
            return toml.load(f)
    except (FileNotFoundError, toml.TOMLDecodeError):
        pass


def main(args=None):
    description = '''
    Show type sizes in Rust code. Compiles the code using
    `cargo +nightly rustc <args> -- -Zprint-type-sizes`
    and parses the compiler output to obtain sizes of types.
    All unlisted arguments are passed as `<args>` to `cargo rustc`.
    '''
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--touch', default='src/main.rs',
                        help='Touch this file to force re-linking')
    parser.add_argument('--output', default='html', choices=['html', 'pprint'],
                        help='Output type')
    parser.add_argument('--output-dir', default='./type-sizes', help='HTML output directory')
    parser.add_argument('--sort-size', action='store_true', help='Sort by size')
    parser.add_argument('--max-length', type=int, default=120,
                        help='Limit length of type names (0 to disable)')
    parser.add_argument('--include', action='append', help='Include only types matching regex')
    parser.add_argument('--exclude', action='append', help='Exclude types that match regex')
    parser.add_argument('--exclude-std', action='append_const', dest='exclude',
                        const='^(std|core)::', help='Exclude types from std:: and core::')
    args, tail = parser.parse_known_args()

    touch(args.touch)

    log.info('Compiling ...')
    proc, cmd = compile(tail)

    log.info('Parsing ...')
    types = parse(proc.stdout.split('\n'))

    # Filter types by name
    filters = []
    for regex in args.include or []:
        pattern = re.compile(regex)
        # Avoid Python using "pattern" variable by name instead of the one in this scope
        filters.append(lambda type, pattern=pattern: pattern.search(type.name))
    for regex in args.exclude or []:
        pattern = re.compile(regex)
        filters.append(lambda type, pattern=pattern: not pattern.search(type.name))
    for f in filters:
        types = filter(f, types)
    types = list(types)

    if args.max_length > 0:
        for type in types:
            walk_tree(type, lambda node: trim_name(node, max_length=args.max_length))

    package_name = None
    for dir in fs_walk_up():
        data = parse_cargo_toml(dir)
        if data and 'package' in data and 'name' in data['package']:
            package_name = data['package']['name']
            break

    log.info('Generating output ...')

    if args.sort_size:
        types = sorted(types, key=lambda typ: typ.size, reverse=True)

    if args.output == 'pprint':
        for typ in types:
            pprint.pprint(typ)
    elif args.output == 'html':
        input_template = 'index.jinja2.html'
        input_static = ['index.js', 'styles.css']
        output = 'index.html'

        this_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))

        if not os.path.exists(args.output_dir):
            os.mkdir(args.output_dir)

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(this_dir),
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=True,
        )
        template = env.get_template(input_template)
        template.stream(
            types=types,
            package_name=package_name or '_unknown_',
            command=' '.join(cmd),
            datetime=datetime.datetime.now(),
        ).dump(os.path.join(args.output_dir, output))

        for file in input_static:
            shutil.copyfile(os.path.join(this_dir, file), os.path.join(args.output_dir, file))

        log.info('HTML output saved to %s', os.path.join(args.output_dir, output))
    else:
        raise ValueError(args.output)


if __name__ == "__main__":
    main()
