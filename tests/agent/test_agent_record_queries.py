"""Family SQL uses record identities, current links and bounded read-only results."""

from contextlib import closing
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from agent.dataset_helpers import synthetic_datasets, seed_record_links
from elbow_helper.agent.datasets.catalogue import SOURCES
from elbow_helper.agent.datasets.guide import DataGuide
from elbow_helper.agent.datasets.query import run_query
from elbow_helper.agent.datasets.tools import dataset_tools


class RecordQueryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = synthetic_datasets(directory.name)
        self.sources = {source.alias: source for source in SOURCES if source.tables}
        with closing(sqlite3.connect(self.sources['family'].resolve(self.paths))) as conn, conn:
            conn.executescript("""
                INSERT INTO accounts VALUES (1,'#P0','Synthetic',1000000,NULL,'active');
                INSERT INTO accounts VALUES (2,'#P2','Unlinked',1000000,NULL,'active');
                INSERT INTO members VALUES (1,99,'synthetic','Synthetic',NULL,1000000,2000000,0,'present');
                INSERT INTO members VALUES (2,88,'previous','Previous',NULL,1000000,2000000,0,'present');
                INSERT INTO account_links (account_id,member_id,from_at,to_at,last_observed_at,basis,source_item_id)
                    VALUES (1,2,0,1000000,1000000,'observed',1),(1,1,1000000,NULL,2000000,'observed',1);
                INSERT INTO clans VALUES (1,'#C0','SYN','Synthetic',1,1);
                INSERT INTO clan_membership (account_id,clan_id,clan_role,from_at,last_observed_at,source_item_id)
                    VALUES (1,1,'member',1000000,2000000,1),(2,1,'member',1000000,2000000,1);
                INSERT INTO wars (id,type,war_tag,end_at,state,team_size,attacks_per_member,first_seen_at,
                    last_seen_at,roster_complete,attacks_complete,result_complete,content_hash,field_presence_json,source_run_id)
                    VALUES (1,'CWL','#W0',2000000,'warEnded',1,1,1000000,2000000,1,1,1,'hash','{}',1);
                INSERT INTO war_sides (war_id,side,clan_tag,attacks,extra_json,source_run_id)
                    VALUES (1,0,'#C0',1,'{}',1),(1,1,'#C2',0,'{}',1);
                INSERT INTO war_members (id,war_id,side,player_tag,account_id,townhall,attacks_expected,
                    attacks_used,extra_json,source_run_id)
                    VALUES (1,1,0,'#P0',1,16,1,1,'{}',1),(2,1,1,'#P9',NULL,16,1,0,'{}',1);
                INSERT INTO war_attacks VALUES (1,1,1,2,3,100,120,'{}',1);
                INSERT INTO cwl_seasons VALUES (1,'2026-10','synthetic','ended',NULL,NULL,1000000,2000000,'{}',1);
                INSERT INTO cwl_clans VALUES (1,'#C0','Synthetic',1,NULL,'{}',1);
                INSERT INTO cwl_rounds VALUES (1,1,'#W0',1,1);
            """)
        seed_record_links(self.paths, [("#P0", 99)])

    def query(self, sql, params=None, **kwargs):
        result, touched = run_query(SOURCES, self.paths, set(), sql, params, **kwargs)
        self.assertFalse(touched)
        return result

    def test_wars_join_current_record_owners_and_tag_links_without_multiplying_accounts(self):
        result = self.query("""
            SELECT a.player_tag,m.discord_id,k.discord_user_id,COUNT(t.attack_order) AS attacks
            FROM family.accounts a JOIN family.war_members p ON p.account_id=a.id
            JOIN family.wars w ON w.id=p.war_id
            JOIN family.war_sides s ON s.war_id=p.war_id AND s.side=p.side
            JOIN family.clans c ON c.tag=s.clan_tag AND c.is_family=1
            JOIN family.clan_membership cm ON cm.account_id=a.id AND cm.clan_id=c.id AND cm.to_at IS NULL
            LEFT JOIN family.account_links l ON l.account_id=a.id AND l.to_at IS NULL
            LEFT JOIN family.members m ON m.id=l.member_id
            LEFT JOIN links.links k ON k.player_tag=a.player_tag
            LEFT JOIN family.war_attacks t ON t.attacker_member_id=p.id AND t.war_id=w.id
            WHERE w.end_at>=:after GROUP BY a.id
        """, {'after': 1000000})
        self.assertEqual(result['rows'], [dict(player_tag='#P0', discord_id=99, discord_user_id=99, attacks=1)])

    def test_unlinked_accounts_remain_in_the_family_population(self):
        result = self.query("""SELECT a.player_tag,m.discord_id FROM family.accounts a
            LEFT JOIN family.account_links l ON l.account_id=a.id AND l.to_at IS NULL
            LEFT JOIN family.members m ON m.id=l.member_id ORDER BY a.id""")
        self.assertEqual(result['rows'], [dict(player_tag='#P0', discord_id=99),
                                          dict(player_tag='#P2', discord_id=None)])

    def test_cwl_view_allows_only_its_declared_dependencies(self):
        result = self.query("""SELECT s.season_key,c.clan_tag,a.round_no,a.stars FROM family.cwl_attacks a
            JOIN family.cwl_seasons s ON s.id=a.season_id
            JOIN family.cwl_clans c ON c.season_id=s.id WHERE s.season_key=:season""", {'season': '2026-10'})
        self.assertEqual(result['rows'], [dict(season_key='2026-10', clan_tag='#C0', round_no=1, stars=3)])
        self.assertIn('error', self.query('SELECT * FROM family.war_collection_state'))

    def test_frozen_tables_are_neither_offered_nor_readable(self):
        guide = DataGuide(self.paths).for_levels(set())
        for name in ('wars', 'war_attacks', 'war_roster_members', 'war_activity', 'player_snapshots', 'report_players',
                     'raid_member_activity', 'player_directory'):
            with self.subTest(table=name):
                self.assertNotIn('health.' + name, guide)
                self.assertIn('error', self.query('SELECT * FROM health.' + name))
        for table, _, note in self.sources['family'].tables:
            self.assertIn('family.' + table + ' [none]: ' + note, guide)
        self.assertIn('end_at INT (Unix us UTC)', guide)

    def test_record_is_read_only_and_schema_failures_return_its_actual_columns(self):
        for sql in ("DELETE FROM family.wars", "UPDATE family.accounts SET current_name='changed'",
                    "DROP VIEW family.cwl_attacks"):
            self.assertIn('error', self.query(sql))
        result = self.query('SELECT wrong FROM family.wars')
        self.assertEqual(result['cause'], 'sql_schema_error')
        self.assertIn('end_at', result['available_schema']['family.wars']['columns'])
        self.assertEqual(self.query('SELECT COUNT(*) AS n FROM family.wars')['rows'], [{'n': 1}])

    def test_missing_record_is_unavailable_and_not_created(self):
        path = self.sources['family'].resolve(self.paths)
        path.unlink()
        self.assertNotIn('family.wars [', DataGuide(self.paths).for_levels(set()))
        self.assertIn('error', self.query('SELECT * FROM family.wars'))
        self.assertFalse(path.exists())

    def test_default_read_returns_a_whole_family_and_explicit_smaller_limit_reports_the_cut(self):
        with closing(sqlite3.connect(self.sources['family'].resolve(self.paths))) as conn, conn:
            conn.executemany("INSERT INTO accounts VALUES (?,?,NULL,1,NULL,'active')",
                             [(i, '#S' + str(i)) for i in range(3, 701)])
        sql = 'SELECT player_tag FROM family.accounts ORDER BY id'
        result = self.query(sql)
        self.assertEqual(result['row_count'], 700)
        self.assertFalse(result['truncated'])
        self.assertEqual(dataset_tools()[0].definition.parameters['properties']['max_rows']['default'], 1000)
        result = self.query(sql, max_rows=200)
        self.assertEqual((result['row_count'], result['total_rows'], result['rows_cut']), (200, 700, 500))
        self.assertIn('incomplete', result['instruction'])

    def test_war_json_omits_history_and_preserves_feature_state_and_source_file(self):
        source = next(s for s in SOURCES if s.state_name == 'war_cache')
        path = source.resolve(self.paths)
        original = json.dumps({'processed_wars': ['one'], 'war_board_history': {'private': 'old war'},
                               'war_role_state': {'SYN': {'managed': [99]}}, 'summary_messages': {}})
        path.write_text(original, encoding='utf-8')
        result = self.query('SELECT doc FROM state.war_cache')
        self.assertEqual(json.loads(result['rows'][0]['doc']), {
            'processed_wars': ['one'], 'war_role_state': {'SYN': {'managed': [99]}}, 'summary_messages': {}})
        self.assertEqual(path.read_text(encoding='utf-8'), original)
        path.write_text('[]', encoding='utf-8')
        self.assertIn('unavailable', self.query('SELECT doc FROM state.war_cache')['error'])
