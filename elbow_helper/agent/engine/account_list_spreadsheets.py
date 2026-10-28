"""Prepare assessment lists through the ordinary spreadsheet delivery path."""
import logging
import re

from ..access import AgentAccessLost, require_evidence_access
from ..files.delivery import delivery_manifest
from ..files.spreadsheet_tools import _store_spreadsheet
from ..files.spreadsheets import AgentSpreadsheet, AgentSpreadsheetSheet, valid_text
from .account_lists import account_values


LOGGER = logging.getLogger(__name__)


def _shorten_sheet_name(title, maximum):
    title = title.strip().strip("'").strip()
    while len(title) > maximum:
        without_parenthetical = re.sub(r'\s*\([^()]*\)$', '', title).rstrip()
        if not without_parenthetical or without_parenthetical == title:
            break
        title = without_parenthetical
    if len(title) > maximum:
        shortened = title[:maximum]
        if not title[maximum].isspace():
            shortened = re.sub(r'\s+\S*$', '', shortened)
        # A single overlong word has no readable boundary to prefer.
        title = shortened
    return title.rstrip().rstrip("'").rstrip()


def _sheet_name(declaration, used):
    # Titles are model output; account names never participate in tab naming.
    title = declaration.get('title', '').strip() or declaration['id']
    base = ''.join('_' if character in '[]*:/\\?"' else character for character in title)
    base = base.strip().strip("'").strip() or declaration['id']
    name, counter = _shorten_sheet_name(base, 31), 2
    while name.casefold() in used:
        suffix = '_' + str(counter)
        name = _shorten_sheet_name(base, 31 - len(suffix)) + suffix
        counter += 1
    if not valid_text(name, maximum=31):
        raise ValueError('Invalid account list tab title')
    used.add(name.casefold())
    return name


def _estimate(value):
    # Preserve percentage units and approximate/range wording. Only bare model
    # numbers become numeric cells; the shared writer has no percentage format.
    value = value.strip()
    if re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value):
        return float(value) if '.' in value else int(value)
    return value


async def prepare_account_list_spreadsheet(context, title, declarations, index):
    """Confirm retained storage through the manifest; failures leave inline rows."""
    await require_evidence_access(context)
    try:
        if not valid_text(title, maximum=80):
            return None
        sheets, used = [], set()
        for declaration in declarations:
            columns = tuple(column['heading'] for column in declaration['columns'])
            rows = tuple(tuple(_estimate(value) if 'value' in column else value
                               for column, value in zip(declaration['columns'], account_values(declaration, item, index)))
                         for item in declaration['accounts'])
            sheets.append(AgentSpreadsheetSheet(_sheet_name(declaration, used), columns, rows))
        result = await _store_spreadsheet(context, AgentSpreadsheet(title, tuple(sheets)))
        if result.get('attachment_prepared'):
            return any(item['filename'] == result['filename']
                       for item in delivery_manifest(context.state.attachments)['files'])
    except AgentAccessLost:
        raise
    except Exception as error:
        LOGGER.warning('Assessment spreadsheet preparation failed: %s', type(error).__name__)
        await require_evidence_access(context)
    return False
