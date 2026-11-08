"""Scripted planners must express family selections using the real visible catalogue."""

from dataclasses import replace
import unittest


from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.engine.catalogue import FAMILY_INSTRUCTION
from elbow_helper.agent.plan.format import plan_definition, system_instructions
from elbow_helper.agent.plan.planning import read_request
from agent.engine.test_agent_plan_flow import _model_step, _Session


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
