"""One collected CWL selection and scoring adapter, called on a worker thread."""

from dataclasses import asdict
from datetime import datetime
import re
import sqlite3

from elbow_helper.configuration.clans import CLANS, CLAN_ORDER
from elbow_helper.domain.cwl import SEASON_KEY
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from .config import CWL_CLAN_CODES
from .results_summary import cwl_analysis


def cwl_clan_error(codes):
    unknown = [code for code in codes if code not in CWL_CLAN_CODES]
    if unknown:
        return {'error': 'Not CWL clans: ' + ', '.join(unknown) + '.', 'cause': 'invalid_arguments',
                'invalid_fields': ['clan_codes']}


class CwlResults:
    """Select first, call the feature scorers, then filter and summarize before paging."""

    def __init__(self, queries):
        self.queries = queries

    def snapshot(self, metric, **selection):
        method = {'performance': 'performance', 'ass': 'ass_wars', 'bonus': 'bonus_wars'}[metric]
        return getattr(self.queries, method)(**selection)

    @staticmethod
    def refusal(arguments):
        metric, clan = arguments.get('metric', 'performance'), arguments.get('clan_codes', [])
        if metric in ('ass', 'bonus') or metric in ('performance', 'mega_ass') and len(clan) == 1:
            refused = cwl_clan_error(clan)
            if refused:
                return refused
        if metric in ('ass', 'bonus'):
            if not clan:
                return {'error': 'Which clan should I score?', 'cause': 'invalid_arguments',
                        'invalid_fields': ['clan_codes']}
            return CwlResults.validate_selection(arguments)

    @staticmethod
    def validate_selection(arguments):
        try:
            after, before = [datetime.fromisoformat(arguments[key]) if arguments.get(key) else None
                             for key in ('after', 'before')]
            if any(value is not None and value.utcoffset() is None for value in (after, before)):
                raise ValueError('Choose timezone-aware dates.')
            if (after is None) != (before is None) or after is not None and before <= after:
                raise ValueError('Choose a complete date window with its end after its start.')
            selectors = ('season', 'season_keys', 'war_ids', 'latest_completed', 'history_limit')
            if sum(bool(arguments.get(key)) for key in selectors) + (after is not None) > 1:
                raise ValueError('Choose one season selection, war selection or date window.')
            for season in arguments.get('season_keys', []) or ([arguments['season']] if arguments.get('season') else []):
                if not isinstance(season, str) or re.fullmatch(SEASON_KEY, season) is None:
                    raise ValueError('Choose a season in YYYY-MM form, or a CWL season key a read returned.')
        except ValueError as error:
            return {'error': str(error), 'cause': 'invalid_arguments'}

    def selection(self, arguments):
        """Ended league wars, preserving the existing season, subset and date-window units."""
        refused = self.validate_selection(arguments)
        if refused:
            return refused
        try:
            after, before = [datetime.fromisoformat(arguments[key]) if arguments.get(key) else None
                             for key in ('after', 'before')]
            selectors = ('season', 'season_keys', 'war_ids', 'latest_completed', 'history_limit')
            if not (after is not None or any(arguments.get(key) for key in selectors)):
                return {'error': 'Which season, wars or date window do you want?', 'cause': 'invalid_arguments',
                        'missing_fields': ['season', 'season_keys', 'after', 'war_ids', 'latest_completed', 'history_limit']}
            return self.queries.attendance(
                clan_codes=arguments.get('clan_codes', list(CLANS)), war_type='CWL',
                after_ts=int(after.timestamp()) if after else None,
                before_ts=int(before.timestamp()) if before else None,
                season=arguments.get('season'), war_ids=arguments.get('war_ids', ()),
                **{key: arguments[key] for key in ('season_keys', 'history_limit', 'latest_completed')
                   if arguments.get(key)})
        except ValueError as error:
            return {'error': str(error), 'cause': 'invalid_arguments'}
        except (OSError, sqlite3.Error):
            return {'error': 'Stored war evidence is unavailable.', 'cause': 'source_unavailable'}

    def read(self, arguments):
        metric = arguments.get('metric', 'performance')
        clan = arguments.get('clan_codes', [])
        refused = self.refusal(arguments)
        if refused:
            return refused
        if metric in ('ass', 'bonus') and len(clan) > 1:
            # Options every clan would refuse alike are one mistake to correct, not a gap per clan.
            rows, summaries, gaps, population = [], [], [], 0
            for code in clan:
                part = self.read({**{key: value for key, value in arguments.items()
                    if key != 'top_n'}, 'clan_codes': [code]})
                if 'error' in part:
                    gaps.append({'clan_code': code, 'cause': part.get('cause'), 'error': part['error']})
                    continue
                rows.extend(dict(row, clan_code=code) for row in part['rows'])
                population += part.get('ranked_population', len(part['rows']))
                summaries.append({'clan_code': code, **{key: value for key, value in part.items()
                    if key not in ('rows', 'unobserved_accounts', 'selected_group_metrics',
                                   'score_comparisons', 'analysis_scope')}})
            if gaps and not summaries and all(gap['cause'] == 'invalid_arguments' for gap in gaps):
                return {'error': gaps[0]['error'], 'cause': 'invalid_arguments'}
            selected_rows = rows[:arguments['top_n']] if arguments.get('top_n') else rows
            for summary in summaries:
                summary['top_rows'] = [row for row in selected_rows
                    if row['clan_code'] == summary['clan_code'] and row.get('rank') is not None][:3]
            return {'rows': selected_rows,
                    **cwl_analysis(rows, metric, scopes=summaries),
                    'metric': metric, 'clan_results': summaries, 'missing_sources': gaps,
                    'ranked_population': population, 'status': 'partial' if gaps else 'resolved',
                    'unobserved_accounts': sorted(set(arguments.get('account_tags', []))
                        - {row['player_tag'] for row in rows}),
                    'rank_scope': 'separate ranking for each selected clan and season',
                    'coverage': 'stored completed CWL wars; per-clan scoring settings and gaps remain explicit'}
        try:
            if arguments.get('season_keys'):
                if metric == 'mega_ass':
                    return {'error': 'Choose the MEGA ASS history depth rather than separate season scores.', 'cause': 'invalid_arguments'}
                parts = []
                for season in sorted(arguments['season_keys'], reverse=True):
                    part = self.read({**{key: value for key, value in arguments.items()
                        if key not in ('season_keys', 'top_n')}, 'season': season})
                    if 'error' in part:
                        return part
                    parts.append(part)
                rows = [dict(row, season=row.get('season') or season) for season, part in
                        zip(sorted(arguments['season_keys'], reverse=True), parts) for row in part['rows']]
                return {'rows': rows[:arguments['top_n']] if arguments.get('top_n') else rows,
                        'metric': metric, 'component_seasons': sorted(arguments['season_keys'], reverse=True),
                        'ranked_population': sum(part.get('ranked_population', len(part['rows'])) for part in parts),
                        'clan_seasons': [row for part in parts for row in part.get('clan_seasons', [])],
                        'clan_totals': [row for part in parts for row in part.get('clan_totals', [])],
                        'missing_sources': [row for part in parts for row in part.get('missing_sources', [])],
                        'attendance_coverage': [part['attendance_coverage'] for part in parts if 'attendance_coverage' in part],
                        'unobserved_accounts': sorted(set(arguments.get('account_tags', [])) - {row['player_tag'] for row in rows}),
                        'coverage': [part.get('coverage') for part in parts], 'score_scope': 'separate season results'}
            if metric in ('ass', 'bonus'):
                if len(clan) != 1:
                    return {'error': 'Which clan should I score?', 'cause': 'invalid_arguments', 'invalid_fields': ['clan_codes']}
                if not any(arguments.get(key) for key in (
                        'season', 'after', 'before', 'war_ids', 'latest_completed')):
                    season = self.queries.latest_season(clan_code=clan[0])
                    if season is None:
                        return {'rows': [], 'metric': metric, 'seasons': [],
                                'coverage': 'No stored completed CWL wars for the selected clan.'}
                    arguments = {**arguments, 'season': season}
                selected = ({'war_ids': arguments['war_ids']} if arguments.get('war_ids')
                            else self.selection(arguments))
                if 'error' in selected:
                    return selected
                if not selected['war_ids']:
                    return {'rows': [], 'coverage': selected['coverage'], 'metric': metric}
                snapshot = self.snapshot(metric, clan_code=clan[0], war_ids=selected['war_ids'])
                data = asdict(snapshot)
                rows = data.pop('rows' if metric == 'ass' else 'summaries')
                data.pop('attacks', None)
                rows = sorted(rows, key=lambda row: (row.get('rank') is None,
                             row.get('rank') or 0, row['player_tag']))
            else:
                if arguments.get('after') or arguments.get('before') or arguments.get('war_ids') or arguments.get('latest_completed'):
                    return {'error': 'Choose ASS or bonus for an explicit subset of wars.', 'cause': 'invalid_arguments'}
                snapshot = self.snapshot('performance',
                    history_limit=arguments.get('history_limit', 3), season=arguments.get('season'),
                    clan_code=clan[0] if len(clan) == 1 else None)
                data = asdict(snapshot)
                rows = [row for row in data.pop('rows') if not clan or row['clan_code'] in clan]
                season = arguments.get('season')
                complete_groups = {(row['season'], row['clan_code']) for row in rows}
                missing_clans = [code for code in clan or list(CLANS)
                                 if season and (season, code) not in complete_groups]
                if missing_clans:
                    partial = self.selection({**arguments, 'clan_codes': missing_clans})
                    data['missing_sources'] = list(data.get('missing_sources', []))
                    if 'error' not in partial:
                        data['seasons'] = tuple(dict.fromkeys((*data.get('seasons', ()), season)))
                        data['attendance_coverage'] = partial['coverage']
                        data['clan_seasons'] = list(data.get('clan_seasons', []))
                    for code in missing_clans:
                        if 'error' in partial:
                            data['missing_sources'].append({'source': 'CWL performance', 'clan_code': code,
                                'season': season, 'cause': partial['cause']})
                            continue
                        source_rows = [row for row in partial['rows'] if row['clan_code'] == code]
                        attendance_rows = aggregate_attendance(source_rows, {}, 'account')
                        rows.extend(dict(row, clan_code=code, season=season, score=None, rank=None,
                                         scoring_status='incomplete_season', league=None) for row in attendance_rows)
                        war_ids = {row['war_id'] for row in source_rows}
                        war_ids.update(row['war_id'] for row in partial.get('war_aliases', []) if row['clan_code'] == code)
                        data['clan_seasons'].append({'clan_code': code, 'season': season, 'wars': len(war_ids),
                            'accounts': len(attendance_rows), 'complete': False, 'league': None,
                            'coverage': partial['coverage']})
                        data['missing_sources'].append({'source': 'CWL performance', 'clan_code': code,
                            'season': season, 'cause': 'incomplete_season' if war_ids else 'missing_history'})
                if metric == 'mega_ass':
                    unique = {}
                    for row in rows:
                        unique.setdefault((row['clan_code'], row['player_tag']), row)
                    rows = sorted(unique.values(), key=lambda row: (row.get('multi_season_rank') is None,
                                 row.get('multi_season_rank') or 0, row['player_tag']))
            if arguments.get('sheet_by') == 'clan_code' and len(clan) == 1:
                rows = [dict(row, clan_code=clan[0]) for row in rows]
            full_count = len(rows)
            rows = [row for row in rows if not arguments.get('account_tags') or row['player_tag'] in arguments['account_tags']]
            unobserved = sorted(set(arguments.get('account_tags', [])) - {row['player_tag'] for row in rows})
            analysis = cwl_analysis(rows, metric, scopes=[{**data,
                'clan_code': clan[0] if len(clan) == 1 else None}])
            if metric == 'mega_ass':
                rows.sort(key=lambda row: (CLAN_ORDER.index(row['clan_code']), row.get('multi_season_rank') is None,
                                          row.get('multi_season_rank') or 0, row['player_tag']))
            elif metric not in ('ass', 'bonus'):
                rows.sort(key=lambda row: (tuple(data.get('seasons', ())).index(row['season']),
                    CLAN_ORDER.index(row['clan_code']), row.get('rank') is None,
                    row.get('rank') or 0, row['player_tag']))
            if arguments.get('top_n'):
                rows = rows[:arguments['top_n']]
            return {**data, **analysis, 'rows': rows, 'metric': metric, 'ranked_population': full_count,
                    'unobserved_accounts': unobserved,
                    'rank_scope': 'selected clan, season and completed wars before account filtering',
                    'coverage': 'stored completed CWL wars; missing seasons remain unknown'}
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
            return {'error': str(error), 'cause': 'source_unavailable'}
