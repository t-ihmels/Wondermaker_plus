#!/usr/bin/env python3
"""Static Klipper config checks, without loading extras or touching hardware.

Usage: uv run --with jinja2 python tools/validate_klipper_config.py [PATH ...]
Defaults to config/live/*.cfg and klipper_extras/*.cfg. Vendor baselines are
historical snapshots, not deployment inputs. Pass other files/directories to
check them explicitly. This cannot prove arbitrary runtime Jinja values or
hardware/module-specific settings are valid; Klipper still validates on load.
"""
import argparse
import ast
import configparser
from dataclasses import dataclass
import fnmatch
from pathlib import Path
import posixpath
import re
import shlex
import sys
import unicodedata

import jinja2

ROOT = Path(__file__).resolve().parents[1]
ENV = jinja2.Environment('{%', '%}', '{', '}')
EXTENDED = re.compile(r'^\s*(?:N[0-9]+\s*)?(?P<cmd>[a-zA-Z_][a-zA-Z0-9_]+)(?:\s+|$)(?P<args>[^#*;]*?)\s*(?:[#*;].*)?$')
OPTION = re.compile(r'^([ \t]*)([^\s:=][^:=]*?)\s*[:=](.*)$')
SECTION = re.compile(r'^\s*\[([^\]]+)\]')
SMART_PUNCTUATION = set('‘’“”−–—＝：；＃＊')
SIDE_EFFECTS = {'action_respond_info', 'action_raise_error', 'action_emergency_stop', 'action_call_remote_method'}


def string_literal(token):
    # Jinja permits multiline quoted strings; Python literal_eval does not.
    return ENV.parse('{' + token + '}').body[0].nodes[0].value


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    code: str
    message: str

    def __str__(self):
        return f'{self.path}:{self.line}: {self.code}: {self.message}'


def config_line(line):
    # Klipper removes # before ConfigParser sees the text, even inside quotes.
    line = line.split('#', 1)[0]
    # ConfigParser's inline semicolon comments require preceding whitespace.
    return re.sub(r'(^|(?<=\s));.*$', '', line)


def options(text):
    """Yield option source and original line numbers; preserve blank/comment lines."""
    section, option, start, indent, lines = '', None, 0, 0, []
    for number, raw in enumerate(text.splitlines(), 1):
        line = config_line(raw)
        if not line.strip():
            if option is not None:
                lines.append('')
            continue
        width = len(line) - len(line.lstrip(' \t'))
        if option is not None and width > indent:
            lines.append(line)
            continue
        if option is not None:
            yield section, option, start, '\n'.join(lines)
            option = None
        match = SECTION.match(line)
        if match:
            section = match[1]
            continue
        match = OPTION.match(line)
        if match:
            indent, option, start = len(match[1]), match[2].strip().lower(), number
            lines = [match[3]]
    if option is not None:
        yield section, option, start, '\n'.join(lines)


def mask_quotes(text):
    quote, escaped, out = None, False, []
    for char in text:
        if escaped:
            out.append(' ' if quote else char)
            escaped = False
        elif char == '\\':
            escaped = True
            out.append(' ')
        elif quote:
            if char == quote:
                quote = None
            out.append(' ')
        elif char in "\"'":
            quote = char
            out.append(' ')
        else:
            out.append(char)
    return ''.join(out)


def template_skeleton(source):
    """Keep literal commands and quotes; replace runtime fields with safe atoms.

    Jinja action messages bypass the G-code parser. Delimiters in their Python
    strings are safe unless config comment stripping already damaged the code.
    """
    tokens = list(ENV.lex(source))
    output, emitted_strings = [], []
    index = 0
    while index < len(tokens):
        line, kind, value = tokens[index]
        if kind == 'data':
            output.append(value)
            index += 1
            continue
        if kind not in ('variable_begin', 'block_begin', 'comment_begin'):
            index += 1
            continue
        ending = {'variable_begin': 'variable_end', 'block_begin': 'block_end', 'comment_begin': 'comment_end'}[kind]
        group, index = [], index + 1
        while index < len(tokens) and tokens[index][1] != ending:
            group.append(tokens[index])
            index += 1
        tail = tokens[index][2] if index < len(tokens) else ''
        index += 1
        full = value + ''.join(token[2] for token in group) + tail
        replacement = ''
        useful = [token for token in group if token[1] != 'whitespace']
        action = useful and useful[0][1] == 'name' and useful[0][2] in SIDE_EFFECTS
        if kind == 'variable_begin' and not action:
            replacement = 'WMP_DYNAMIC'
            before = ''.join(output).rsplit('\n', 1)[-1]
            if before.strip() and before[-1:].isspace():
                # Whole runtime argument lists (rawparams, park_x, etc.) are
                # opaque. Model a valid key=value token, not a bare argument.
                replacement = 'WMP_DYNAMIC=0'
            for token_line, token_kind, token_value in group:
                if token_kind == 'string':
                    try:
                        literal = string_literal(token_value)
                    except (ValueError, SyntaxError):
                        continue
                    emitted_strings.append((token_line, literal))
            if len(useful) == 1 and useful[0][1] == 'string':
                replacement = string_literal(useful[0][2])
        output.append(replacement + '\n' * full.count('\n'))
    return ''.join(output), emitted_strings


def validate_text(path, text):
    text = text.replace('\r\n', '\n')
    errors = []
    def add(line, code, message):
        errors.append(Diagnostic(str(path), line, code, message))
    for number, line in enumerate(text.split('\n'), 1):
        for char in line:
            if (unicodedata.category(char) in ('Cc', 'Cf') and char != '\t'):
                add(number, 'character', f'Unexpected U+{ord(char):04X} ({unicodedata.name(char, "control character")})')
        leading = re.match(r'^\s*', line)[0]
        if any(char not in ' \t\r' for char in leading):
            add(number, 'whitespace', 'Use ASCII spaces/tabs for indentation')
    clean = '\n'.join(config_line(line) for line in text.splitlines())
    parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    try:
        parser.read_string(clean, source=str(path))
    except configparser.Error as error:
        number = getattr(error, 'lineno', None)
        if number is None:
            number = getattr(error, 'errors', [(1, '')])[0][0]
        add(number, 'config-syntax', str(error))
        return errors
    for section, option, first_line, source in options(text):
        if section.startswith('gcode_macro ') and option.startswith('variable_'):
            try:
                ast.literal_eval(source.strip())
            except (ValueError, SyntaxError) as error:
                add(first_line, 'variable', f'[{section}] {option} must be a Python literal: {error}')
        if option != 'gcode' and not option.endswith('_gcode'):
            continue
        try:
            ENV.parse(source)
            skeleton, strings = template_skeleton(source)
        except jinja2.TemplateSyntaxError as error:
            add(first_line + error.lineno - 1, 'jinja-syntax', f'[{section}] {option}: {error.message}')
            continue
        lines = skeleton.splitlines()
        for number, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(';'):
                continue
            bare = mask_quotes(line)
            # Display-message text can legitimately contain Unicode punctuation.
            if re.match(r'^(?:N\d+\s*)?M11[78](?:\s|$)', stripped, re.I):
                continue
            if any(char in SMART_PUNCTUATION for char in bare):
                add(first_line + number, 'punctuation', f'[{section}] Non-ASCII punctuation outside a quoted value')
            if any(char.isspace() and char not in ' \t\r' for char in bare):
                add(first_line + number, 'whitespace', f'[{section}] Non-ASCII whitespace in command syntax')
            # Traditional M/G/T commands have a different parameter grammar.
            if re.match(r'^(?:N\d+\s*)?[A-Za-z]\d', stripped):
                continue
            match = EXTENDED.match(stripped)
            if match is None:
                add(first_line + number, 'gcode-syntax', f'[{section}] Malformed command: {stripped}')
                continue
            try:
                for argument in shlex.split(match['args']):
                    key, _ = argument.split('=', 1)
                    if not key:
                        raise ValueError('Empty parameter name')
            except ValueError as error:
                add(first_line + number, 'gcode-arguments',
                    f'[{section}] {match["cmd"]}: {error}. Klipper truncates arguments at #, * or ; even inside quotes')
        for number, literal in strings:
            line = lines[number - 1].strip() if number <= len(lines) else ''
            if (isinstance(literal, str) and any(char in literal for char in '#*;')
                    and line and not re.match(r'^[A-Za-z]\d', line)):
                add(first_line + number - 1, 'emitted-delimiter',
                    f'[{section}] Emitted Jinja string contains a G-code delimiter (#, * or ;)')
    return errors


def validate_sources(sources, check_includes=True):
    errors = []
    sources = {str(path): text for path, text in sources.items() if str(path).endswith('.cfg')}
    for path, text in sources.items():
        errors.extend(validate_text(path, text))
    if not check_includes:
        return errors
    edges = {path: [] for path in sources}
    for path, text in sources.items():
        for number, line in enumerate(text.splitlines(), 1):
            match = re.match(r'^\s*\[include\s+([^\]]+)\]\s*$', config_line(line).strip())
            if not match:
                continue
            pattern = posixpath.normpath(posixpath.join(posixpath.dirname(path), match[1].strip()))
            matches = sorted(name for name in sources if fnmatch.fnmatchcase(name, pattern))
            if not matches and not any(char in pattern for char in '*?['):
                errors.append(Diagnostic(path, number, 'include', f'Missing include: {match[1]}'))
            edges[path].extend((name, number) for name in matches)
    done, active = set(), set()
    def visit(path):
        if path in done:
            return
        active.add(path)
        for target, number in edges[path]:
            if target in active:
                errors.append(Diagnostic(path, number, 'include-cycle', f'Recursive include: {target}'))
            else:
                visit(target)
        active.remove(path)
        done.add(path)
    for path in sources:
        visit(path)
    return errors


def default_paths():
    return sorted((ROOT / 'config/live').glob('*.cfg')) + sorted((ROOT / 'klipper_extras').glob('*.cfg'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='*', type=Path, help='Klipper .cfg files or directories (searched recursively)')
    args = parser.parse_args(argv)
    paths = []
    for path in args.paths:
        paths.extend(sorted(path.rglob('*.cfg')) if path.is_dir() else [path])
    paths = paths if args.paths else default_paths()
    sources, errors = {}, []
    for path in dict.fromkeys(paths):
        if path.suffix != '.cfg':
            errors.append(Diagnostic(str(path), 1, 'input', 'Expected a Klipper .cfg file'))
            continue
        try:
            sources[str(path.resolve())] = path.read_bytes().decode('utf-8')
        except (OSError, UnicodeError) as error:
            errors.append(Diagnostic(str(path), 1, 'read', str(error)))
    if not sources:
        errors.append(Diagnostic('<input>', 1, 'input', 'No Klipper .cfg files selected'))
    errors.extend(validate_sources(sources))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f'Validation failed: {len(errors)} error(s)', file=sys.stderr)
        return 1
    print(f'Validated {len(sources)} Klipper config files')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
