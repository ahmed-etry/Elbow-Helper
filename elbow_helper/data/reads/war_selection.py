"""Bounded regular-war selection and compact account-war facts in one snapshot."""

from dataclasses import dataclass
from datetime import datetime, timezone

from .core import placeholders

SECOND = 1_000_000
CHUNK = 365 * 86400 * SECOND
CAPTURE_WINDOW = 72 * 3600 * SECOND
FLOOR = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()) * SECOND
MAX_SELECTED_WARS = 10_000


@dataclass(frozen=True)
class RegularSelection:
    clans: tuple[str, ...]
    after: int | None
    before: int | None
    war_ids: tuple[str, ...]
    latest_completed: int | None
    as_of: int


def regular_id(view):
    """The clan-facing legacy ID, also used when no old source supplied an alias."""
    end = view['end_at']
    moment = datetime.fromtimestamp(end // SECOND, timezone.utc)
    return f"REG:{view['clan_tag']}:{moment:%Y%m%dT%H%M%S}.{end % SECOND // 1000:03d}Z:{view['opponent']['clan_tag']}"


def regular_aliases(view):
    prefix = 'REG:' + view['clan_tag'] + ':'
    return sorted({regular_id(view), *(row['legacy_id'] for row in view['legacy_ids']
                                     if row['legacy_id'].startswith(prefix))})


def id_window(value):
    parts = value.split(':')
    if len(parts) != 4 or parts[0] != 'REG':
        return None
    try:
        end = datetime.strptime(parts[2], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end = round(end.timestamp() * SECOND)
    return parts[1], max(0, end - CAPTURE_WINDOW), end + CAPTURE_WINDOW + 1


def compact_member(row, view):
    """Final counts and measured attacks stay separate, including copied attack stubs."""
    expected, used = row['attacks_expected'], row['attacks_used']
    complete = (row['missed'] is not None and type(expected) is int and expected > 0
                and type(used) is int and 0 <= used <= expected)
    own = view['coverage']['own']
    causes = [cause for cause, missing in (
        ('not_final', view['state'] != 'warEnded'), ('lineup_incomplete', not own['lineup_complete']),
        ('attacks_incomplete', not own['attacks_complete'])) if missing]
    measured = row['attacks_measured']
    return {'war_id': view['war_id'], 'clan_tag': view['clan_tag'], 'type': view['type'],
            'start_at': view['start_at'], 'end_at': view['end_at'],
            'player_tag': row['player_tag'], 'player_name': row['player_name'],
            'attacks_expected': expected if complete else None, 'attacks_used': used,
            'missed': expected - used if complete else None, 'final_capture': complete,
            'uncertainty_reasons': [] if complete else causes,
            'observed_attack_performance': {
                'attacks': measured, 'stars': row['stars'], 'stars_measured': measured, 'triples': row['triples'],
                'destruction_total': row['destruction'], 'destruction_measured': measured}}


def select_regular(connection, request):
    # Import here keeps WarReads' general selectors independent of this composition.
    from .wars import member_rows, select_wars, war_view, window_coverage

    selected, identities, checked, friendly = {}, set(), [], set()
    requested = set(request.war_ids)

    def keep(war, clan):
        key = war['id'], clan
        if key in selected:
            return
        view = war_view(connection, war, clan, request.as_of)
        view['selection_id'], view['aliases'] = regular_id(view), regular_aliases(view)
        selected[key] = (war, view)
        identities.add(war['id'])
        if len(identities) > MAX_SELECTED_WARS:
            raise ValueError('The selection exceeds the regular-war capacity')

    def scan(clan, after, before, *, enough=None, count_friendly=False):
        end, found = before, set()
        while end > after and (enough is None or len(found) < enough):
            start = max(after, end - CHUNK)
            wars = select_wars(connection, [clan], start, end, ('REG',), request.as_of)
            for row, facing, _, _ in wars:
                if requested:
                    view = war_view(connection, row, facing, request.as_of)
                    opposite = {**view, 'clan_tag': view['opponent']['clan_tag'],
                                'opponent': {'clan_tag': facing}}
                    if not requested.intersection((*regular_aliases(view), regular_id(opposite))):
                        continue
                keep(row, facing)
                found.add(row['id'])
            if count_friendly:
                friendly.update(row['id'] for row, _, _, _ in select_wars(
                    connection, [clan], start, end, ('FRIENDLY',), request.as_of))
            end = start

    if requested:
        # Resolve stored aliases directly: a shifted legacy end need not fall inside the capture window.
        for row in connection.execute(
                f"SELECT DISTINCT w.*,s.clan_tag AS facing FROM wars w JOIN war_sides s ON s.war_id=w.id "
                f"WHERE s.clan_tag IN ({placeholders(request.clans)}) AND w.type='REG' AND w.first_seen_at<=? "
                f"AND w.end_at<=? AND (CAST(w.id AS TEXT) IN ({placeholders(requested)}) OR EXISTS "
                f"(SELECT 1 FROM war_legacy_ids l WHERE l.war_id=w.id AND l.legacy_id IN ({placeholders(requested)})))",
                (*request.clans, request.as_of, request.as_of, *sorted(requested), *sorted(requested))):
            keep(row, row['facing'])
        windows = set()
        for identifier in requested:
            parsed = id_window(identifier)
            if parsed is not None:
                clan, after, before = parsed
                for facing in set((clan, identifier.split(':')[3])).intersection(request.clans):
                    windows.add((facing, after, before))
        for clan, after, before in sorted(windows):
            scan(clan, after, before)
        if identities:
            for row in connection.execute(
                    f"SELECT w.*,s.clan_tag AS facing FROM wars w JOIN war_sides s ON s.war_id=w.id "
                    f"WHERE w.id IN ({placeholders(identities)}) AND s.clan_tag IN ({placeholders(request.clans)})",
                    (*sorted(identities), *request.clans)):
                keep(row, row['facing'])
    else:
        for clan in request.clans:
            scan(clan, FLOOR if request.latest_completed else request.after,
                 request.as_of + 1 if request.latest_completed else request.before,
                 enough=request.latest_completed, count_friendly=request.latest_completed is None)

    entries = sorted(selected.values(), key=lambda item: (-item[1]['end_at'], item[1]['clan_tag'], item[1]['war_id']))
    if request.latest_completed:
        # Choose physical wars once, retaining both selected clan-facing lineups for each war.
        ids = list(dict.fromkeys(view['war_id'] for _, view in entries))[:request.latest_completed]
        entries = [(war, view) for war, view in entries if view['war_id'] in ids]
    rows = []
    for war, view in entries:
        checked.append((war, view['clan_tag'], view['coverage']))
        rows.extend(compact_member(row, view) for row in member_rows(connection, war, view['clan_tag'], view['coverage']))
    return {'wars': [view for _, view in entries], 'rows': rows, 'as_of': request.as_of,
            'friendly_wars_not_counted': len(friendly) if not requested and not request.latest_completed else None,
            'coverage': window_coverage(connection, request.clans, ('REG',), request.after, request.before,
                                        request.as_of, checked, False)}
