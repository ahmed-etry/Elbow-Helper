"""Scripted planners must express family selections using the real visible catalogue."""

from dataclasses import replace
import unittest


from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.catalogue import FAMILY_INSTRUCTION
from elbow_helper.agent.plan.format import capability_list, plan_definition, system_instructions
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.plan.input_rules import RULES
from elbow_helper.agent.plan.rule_format import catalogue_rules, rule_clauses
from elbow_helper.agent.plan.planning import read_request
from tests.agent.engine.test_agent_plan_flow import _model_step, _Session


def step(identifier, capability, arguments, dependencies=()):
    return {'id': identifier, 'capability': capability, 'arguments': arguments,
            'depends_on': list(dependencies)}


def plans(member_id=41):
    """These exact checked plans are also exercised by the GET-only live harness."""
    cases = {}
    for code in ('BEH', 'BEM'):
        cases['leadership.' + code] = [step('holders', 'read_member_cohort',
            {'basis': 'role_holders', 'clan_codes': [code]})]
    cases['war_co'] = [step('holders', 'read_member_cohort',
        {'basis': 'role_holders', 'role_name': 'War Co', 'clan_codes': ['BEH']})]
    cases['roster_roles'] = [
        step('roster', 'read_clan_rosters', {'clan_codes': ['BES'], 'basis': 'current_in_game'}),
        step('home_role', 'read_member_cohort', {'basis': 'role_holders', 'clan_codes': ['BES'], 'clan_role': 'member'}),
        step('owners', 'read_member_accounts', {'cohort_ref': {'step': 'roster', 'path': ['report_id']},
            'include_roles': True}, ['roster']),
    ]
    cases['cwl_workbook'] = [step('ass', 'read_cwl_results', {'metric': 'ass', 'season': '2026-10',
        'clan_codes': ['BES', 'BE1', 'BEM', 'BEC', 'BEE'], 'delivery': 'xlsx', 'sheet_by': 'clan_code'})]
    cases['command_options'] = [step('help', 'read_bot_command_help', {'command_path': '/health clan'})]
    cases['room_permissions'] = [step('access', 'read_discord_access',
        {'role_names': ['Cross-Leader', 'Co-Leader']})]
    cases['role_preview'] = [step('preview', 'add_discord_roles',
        {'role_name': 'Elder', 'member_ids': [member_id]})]
    return {name: {'goal': 'Read the requested structured evidence', 'effort': 'high', 'output': 'text',
                   'steps': steps, **({'self_target': True} if name == 'role_preview' else {})}
            for name, steps in cases.items()}


class CatalogueScript(_Session):
    def __init__(self, testcase, registry, plan, guidance):
        self.testcase, self.registry, self.plan, self.guidance = testcase, registry, plan, guidance
        super().__init__([_model_step(plan)], [])

    async def advance(self, *args, **kwargs):
        # Consume the exact production catalogue, rather than synthetic definitions.
        catalogue = system_instructions(self.registry) + '\n' + FAMILY_INSTRUCTION
        for fragment in self.guidance:
            self.testcase.assertIn(fragment, catalogue)
        definition = plan_definition(self.registry)
        offered = definition.parameters['properties']['steps']['items']['properties']['capability']['enum']
        for item in self.plan['steps']:
            self.testcase.assertIn(item['capability'], offered)
        return await super().advance(*args, **kwargs)


class FamilyPlannerInputTests(unittest.IsolatedAsyncioTestCase):

    async def test_cohort_and_cwl_refusals_are_present_in_both_planning_catalogues(self):
        registry = build_agent_tools()
        plan = {'goal': 'Read dated donations and CWL scores', 'effort': 'high', 'output': 'text', 'steps': [
            step('aug', 'read_member_cohort', {'basis': 'clan_rosters', 'clan_role': 'member',
                'clan_codes': ['BEH'], 'include_activity': True,
                'after': '2026-08-01T00:00:00+00:00', 'before': '2026-09-01T00:00:00+00:00'}),
            step('cwl', 'read_cwl_results', {'metric': 'signup_pool', 'latest_completed': 3}),
        ]}
        checked = check_plan(plan, registry)
        self.assertEqual(set(checked.step_errors), {'aug', 'cwl'})
        selected = {name: registry[name] for name in ('read_member_cohort', 'read_cwl_results')}
        for descriptions in (True, False):
            catalogue = capability_list(selected, include_returns=False, descriptions=descriptions)
            self.assertNotIn('"given_none"', catalogue)
            self.assertIn('@ without report_id', catalogue)
            self.assertIn('basis!=role_holders|~->!role_name,!clan_role', catalogue)
            self.assertIn('metric=signup_pool|assigned_rosters->!(after|before|history_limit|latest_completed|war_ids)',
                          catalogue)
        # Every enforced predicate remains represented, including rules on specialist reads.
        legend, lines = catalogue_rules(registry)
        for name, tool in registry.items():
            schema = tool.definition.parameters
            clauses, covered, guard = rule_clauses(schema)
            represented = [index for clause in clauses for index in clause.source_rules]
            self.assertEqual(sorted([*represented, *covered]), list(range(len(schema.get(RULES, ())))))
            if guard:
                self.assertTrue(all('report_id' in rule.get('unless', ()) for rule in schema[RULES]))
            for index in covered:
                rule = schema[RULES][index]
                self.assertTrue(rule.get('aware') or rule.get('within'))
                for field in rule.get('aware', ()):
                    self.assertEqual(schema['properties'][field].get('format'), 'date-time')
                for field, values in rule.get('within', {}).items():
                    self.assertLessEqual(set(schema['properties'][field]['items']['enum']), set(values))
            for clause in clauses:
                for fragment in (clause.condition, *clause.effects):
                    self.assertIn(fragment, lines[name] + legend)


    def test_cwl_default_period_is_visible_where_the_planner_reads_it(self):
        # A leader asked for one member's CWL reports; the planner, seeing no default, chose a war count.
        registry = build_agent_tools()
        catalogue = capability_list(registry, include_returns=False, descriptions=False,
                                    opened_capabilities=['read_cwl_results'])
        line = next(line for line in catalogue.splitlines() if line.startswith('read_cwl_results'))
        self.assertIn('No period: 3 ended seasons, +latest for account performance', line)

    async def test_inspection_and_confirmation_use_distinct_visible_paths(self):
        registry = {name: tool for name, tool in build_agent_tools().items() if tool.default_visible}
        plan = plans()['role_preview']
        plan['steps'] = [step('delta', 'read_discord_role_change_preview',
            {'role_name': 'Elder', 'member_ids': [41], 'operation': 'add'})]
        session = CatalogueScript(self, registry, plan, ('Read-only eligibility',))
        decision = await read_request(session, registry, 71)
        self.assertEqual(decision.plan, plan)
        # Private passes filter changes while retaining this inspection path.
        private = {name: replace(tool) for name, tool in registry.items() if tool.action_class.value == 'read'}
        self.assertIn('read_discord_role_change_preview', private)
        self.assertNotIn('add_discord_roles', private)
