"""Read-only search and reading of the bot's own Python source."""
import asyncio
import os
import re
from pathlib import Path

import elbow_helper
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.agent.engine.models import RegisteredAgentTool

ROOT = Path(elbow_helper.__file__).resolve().parent
MATCHES_PER_PAGE = 50
LINES_PER_PAGE = 200
BAD_PATH = {'error': "Give a .py file path relative to the bot's package, as search_bot_code shows it.",
            'cause': 'invalid_arguments'}


def _relative(path):
    """A package-relative posix path, or None when it is absolute, climbs out, or enters __pycache__."""
    if not isinstance(path, str) or not path or '\0' in path:
        return None
    given = Path(path)
    if given.is_absolute() or given.drive or path.startswith(('/', '\\')):
        return None
    parts = [part for part in re.split(r'[\\/]+', path) if part and part != '.']
    if not parts or '..' in parts or '__pycache__' in parts:
        return None
    return '/'.join(parts)


def _files():
    """Every Python file under the package, sorted, as (relative path, file)."""
    found = []
    for folder, folders, names in os.walk(ROOT):
        folders[:] = [name for name in folders if name != '__pycache__']
        for name in names:
            file = Path(folder, name)
            if name.endswith('.py') and file.resolve().is_relative_to(ROOT):
                found.append((file.relative_to(ROOT).as_posix(), file))
    return sorted(found)


def _lines(file):
    lines = file.read_text(encoding='utf-8', errors='replace').split('\n')
    return lines[:-1] if lines and lines[-1] == '' else lines


def _top_level_text():
    """The package folders directly under the root, as 'name/' items for error messages."""
    return ', '.join(f'{child.name}/' for child in sorted(ROOT.iterdir())
                     if child.is_dir() and child.name != '__pycache__')


def _folder_listing(folder):
    """The subfolders and .py files directly inside folder, as package-relative paths."""
    folders, files = [], []
    for child in sorted(folder.iterdir()):
        if child.name == '__pycache__' or not child.resolve().is_relative_to(ROOT):
            continue
        relative = child.relative_to(ROOT).as_posix()
        if child.is_dir():
            folders.append(relative)
        elif child.suffix == '.py':
            files.append(relative)
    return folders, files


def _glob_regex(pattern):
    """A regex for a glob: * and ? stay inside one folder, ** crosses folders."""
    parts, index = [], 0
    while index < len(pattern):
        if pattern.startswith('**/', index):
            parts.append('(?:.*/)?')
            index += 3
        elif pattern.startswith('**', index):
            parts.append('.*')
            index += 2
        else:
            char = pattern[index]
            parts.append({'*': '[^/]*', '?': '[^/]'}.get(char, re.escape(char)))
            index += 1
    return re.compile(''.join(parts) + r'\Z')


def _find(pattern, offset):
    """The .py files whose package-relative path (or name, when pattern has no /) matches pattern."""
    matcher = _glob_regex(pattern)
    matches = [relative for relative, _ in _files()
               if matcher.match(relative if '/' in pattern else relative.rsplit('/', 1)[-1])]
    return matches[offset:offset + MATCHES_PER_PAGE], len(matches)


def _search(matcher, folder, offset):
    shown, total, searched = [], 0, 0
    for relative, file in _files():
        if folder and relative != folder and not relative.startswith(folder + '/'):
            continue
        searched += 1
        for number, line in enumerate(_lines(file), 1):
            if matcher.search(line):
                if offset <= total < offset + MATCHES_PER_PAGE:
                    shown.append(f'{relative}:{number}: {line.strip()}')
                total += 1
    return shown, total, searched


async def search_bot_code(context, arguments):
    query, offset = arguments['query'], arguments.get('offset', 0)
    folder = None
    if arguments.get('path') is not None:
        folder = _relative(arguments['path'])
        if folder is None:
            return dict(BAD_PATH)
        folder = folder.removesuffix('/')
    try:
        matcher = re.compile(query if arguments.get('regex') else re.escape(query),
                             re.IGNORECASE if arguments.get('ignore_case') else 0)
    except re.error as error:
        return {'error': f'That is not a valid regular expression: {error}.', 'cause': 'invalid_arguments'}
    shown, total, searched = await asyncio.to_thread(_search, matcher, folder, offset)
    if not searched:
        return {'error': f'No Python files under that path. Top-level folders: {_top_level_text()}.',
                'cause': 'invalid_arguments'}
    result = {'matches': shown, 'total_matches': total, 'offset': offset, 'files_searched': searched}
    if offset + len(shown) < total:
        result.update(next_offset=offset + len(shown), more_matches=total - offset - len(shown))
    return result


async def read_bot_code(context, arguments):
    path = arguments['path']
    relative = '' if path == '' else _relative(path)
    if relative is None:
        return dict(BAD_PATH)
    file = (ROOT / relative).resolve()
    if not file.is_relative_to(ROOT):
        return dict(BAD_PATH)
    if file.is_dir():
        folders, files = await asyncio.to_thread(_folder_listing, file)
        return {'path': file.relative_to(ROOT).as_posix(), 'folders': folders, 'files': files}
    if not file.exists():
        return {'error': f'No file or folder at that path. Top-level folders: {_top_level_text()}.',
                'cause': 'invalid_arguments'}
    if file.suffix != '.py':
        return dict(BAD_PATH)
    lines = await asyncio.to_thread(_lines, file)
    total, start = len(lines), arguments.get('start_line', 1)
    end = min(arguments.get('end_line', total), total)
    if start > total or end < start:
        return {'error': f'{relative} has {total} lines; choose a range inside it.', 'cause': 'invalid_arguments'}
    last = min(end, start + LINES_PER_PAGE - 1)
    result = {'path': file.relative_to(ROOT).as_posix(), 'start_line': start, 'end_line': last,
              'total_lines': total,
              'lines': [f'{number}: {lines[number - 1]}' for number in range(start, last + 1)]}
    if last < end:
        result.update(truncated=True, next_start_line=last + 1)
    return result


async def find_bot_files(context, arguments):
    offset = arguments.get('offset', 0)
    pattern = _relative(arguments['pattern'])
    if pattern is None:
        return {'error': "Give a pattern relative to the bot's package, such as **/*war*.py, without '..' or a leading slash.",
                'cause': 'invalid_arguments'}
    shown, total = await asyncio.to_thread(_find, pattern, offset)
    result = {'files': shown, 'total_matches': total, 'offset': offset}
    if total == 0:
        result['top_level_folders'] = _top_level_text()
    if offset + len(shown) < total:
        result.update(next_offset=offset + len(shown), more_matches=total - offset - len(shown))
    return result


TOOLS = (
    RegisteredAgentTool(AgentToolDefinition(
        'search_bot_code',
        "Search the bot's own Python source for text or a regular expression. Matches come back as path:line: text.",
        {'type': 'object', 'properties': {
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'regex': {'type': 'boolean', 'description': 'Treat query as a regular expression instead of plain text.'},
            'ignore_case': {'type': 'boolean'},
            'path': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                     'description': 'Only search this file or folder, relative to the package.'},
            'offset': {'type': 'integer', 'minimum': 0}},
         'required': ['query'], 'additionalProperties': False},
        usage_note=(f'Case-sensitive unless ignore_case; names in the code are lowercase_with_underscores. A page holds {MATCHES_PER_PAGE} matches; when there are more, '
                    'the result gives total_matches and next_offset. Paths work with read_bot_code.')),
        search_bot_code, contract=CapabilityContract(())),
    RegisteredAgentTool(AgentToolDefinition(
        'read_bot_code',
        "Read one Python file of the bot's own source, with line numbers. A folder path lists its files and subfolders.",
        {'type': 'object', 'properties': {
            'path': {'type': 'string', 'maxLength': 200,
                     'description': "File path relative to the bot's package, as search_bot_code shows it."},
            'start_line': {'type': 'integer', 'minimum': 1},
            'end_line': {'type': 'integer', 'minimum': 1}},
         'required': ['path'], 'additionalProperties': False},
        usage_note=(f'A page holds {LINES_PER_PAGE} lines; when the range is longer, the result gives next_start_line '
                    'to continue from.')),
        read_bot_code, contract=CapabilityContract(())),
    RegisteredAgentTool(AgentToolDefinition(
        'find_bot_files',
        "Find the bot's own Python files by name or path pattern, such as **/*war*.py.",
        {'type': 'object', 'properties': {
            'pattern': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                        'description': 'Glob matched against the path relative to the package. A pattern without / matches file names in any folder.'},
            'offset': {'type': 'integer', 'minimum': 0}},
         'required': ['pattern'], 'additionalProperties': False},
        usage_note=(f'A page holds {MATCHES_PER_PAGE} files; when there are more, the result gives total_matches and next_offset. '
                    'Paths work with read_bot_code.')),
        find_bot_files, contract=CapabilityContract(())),
)
