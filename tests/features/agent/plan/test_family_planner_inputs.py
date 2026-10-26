"""Scripted planners must express family selections using the real visible catalogue."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

import discord

from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.families.catalogue import FAMILY_INSTRUCTION
from elbow_helper.features.agent.plan.format import plan_definition, system_instructions
from elbow_helper.features.agent.plan.planning import read_request
from features.agent.engine.test_agent_plan_flow import _context, _model_step, _Model, _Session


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
    async def test_confirmation_preview_finishes_within_three_total_model_calls(self):
        context = _context()
        context.guild.owner_id = context.member.id
        context.member.guild_permissions = SimpleNamespace(manage_roles=True)
        context.member.mention = '<@41>'
        for role in context.guild.roles:
            role.name = 'Synthetic role'
            role.is_default = lambda: False
        elder = SimpleNamespace(id=123, name='Elder', mention='<@&123>', position=1,
            managed=False, permissions=discord.Permissions.none(), is_default=lambda: False)
        context.guild.roles.append(elder)
        context.guild.get_role = lambda key: elder if key == elder.id else None
        context = replace(context, bot=SimpleNamespace(tree=SimpleNamespace(get_commands=lambda **kw: [])),
            hibernation_queries=SimpleNamespace(is_hibernating=lambda key: False))
        session = _Session([_model_step(plans()['role_preview'])], [])
        model = _Model(session)
        answer = await AgentService(model).answer(question='Prepare my Elder role confirmation preview.',
            local_context='', context=context)
        self.assertIn('add_discord_roles:', model.request['system_prompt'])
        self.assertIn('role_name:str', model.request['system_prompt'])
        self.assertEqual(len(context.state.proposed_changes), 1)
        self.assertEqual(context.state.proposed_changes[0].values, {'role_id': 123, 'member_id': 41})
        self.assertIn('<@&123>', answer)
        self.assertLessEqual(len(model.routing_session.calls) + len(session.calls), 3)

    async def test_every_cause_is_expressible_in_one_initial_plan(self):
        registry = {name: tool for name, tool in build_agent_tools().items() if tool.default_visible}
        guidance = {
            'leadership.BEH': ('clan_role:str=', 'clan_role selects configured roles only'),
            'leadership.BEM': ('clan_role:str=', 'in-game roster ranks do not identify Discord assignments'),
            'war_co': ('role_name:str', 'Do not substitute clan_role=leadership', 'war participant role, not War Co'),
            'roster_roles': ('include_roles:bool(default=false)', 'clan_role=member selects home roles', 'selected_role_ids'),
            'cwl_workbook': ('ASS/bonus accept multiple clan_codes', 'sheet_by:str=clan_code', 'tab per clan'),
            'command_options': ('command_path:str', 'exact slash-command options', 'scope gap'),
            'room_permissions': ('read_discord_access:', 'Omit channel_ids for this room', 'role_names'),
            'role_preview': ('add_discord_roles:', 'role_name:str', 'never applies it', 'queues only actual changes'),
        }
        for name, plan in plans().items():
            with self.subTest(cause=name):
                checks = []
                session = CatalogueScript(self, registry, plan, guidance[name])
                decision = await read_request(session, registry, 71,
                    on_check=lambda _, check: checks.append(check))
                self.assertEqual(decision.plan, plan)
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(checks[0].ok, checks[0].error)
                self.assertFalse(checks[0].step_errors)

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
