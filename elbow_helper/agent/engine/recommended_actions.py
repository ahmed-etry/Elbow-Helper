"""Prepare evidence-bound assessment recommendations through ordinary change plans."""

import json
import logging

from ..actions.contracts import ActionClass, check_bundle
from ..actions.details import prepare_preview
from ..actions.preview import preview_text
from ..plan.checker import check_plan, check_step, has_reference
from ..research.member_provenance import allowed_member_ids, member_target_issue
from .capability_contract import compile_capability_call, entity_kind
from .tool_call import restore_tool_state, tool_state_snapshot


LOGGER = logging.getLogger(__name__)
MAX_RECOMMENDED_ACTIONS = 3
TARGET_KINDS = frozenset({'discord_member', 'clash_account'})
RECOMMENDED_ACTIONS_SCHEMA = {'type': 'array', 'maxItems': MAX_RECOMMENDED_ACTIONS, 'items': {
    'type': 'object', 'properties': {
        'capability': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'target': {'anyOf': [
            {'type': 'object', 'properties': {'member_id': {'type': 'integer', 'minimum': 1}},
             'required': ['member_id'], 'additionalProperties': False},
            {'type': 'object', 'properties': {'player_tag': {
                'type': 'string', 'minLength': 1, 'maxLength': 60}},
             'required': ['player_tag'], 'additionalProperties': False}]},
        'arguments': {'type': 'object', 'additionalProperties': True}},
    'required': ['capability', 'target', 'arguments'], 'additionalProperties': False}}

RECOMMENDED_ACTIONS_INSTRUCTION = """Use recommended_actions only for concrete, unconditional changes
in recommendation that serve the asker's requested decision or operational plan about those
people. Leave recommended_actions empty for factual/statistical answers, unrequested decisions,
unresolved intent, open choices, conditional advice, unsupported targets or operations with no
available capability.
Declare at most three items: capability, target (member_id or player_tag from this request's
evidence), and the capability's complete arguments, including the same target. Use one
target per item, with singleton lists where the capability requires a list. Member provenance
rules still apply; a linked owner or nearby speaker alone is not a resolved action target.
Do not resolve names, invent IDs, change in-game ranks through Discord roles, or substitute
a Discord queue for an in-game move. Only declare capabilities in recommended_action_capabilities.
The server validates these items and prepares the existing preview/Confirm flow; nothing
runs before Confirm. Keep action offers and preparation claims out of the answer prose."""


def action_capabilities(registry):
    """Offer only closed contracts whose affected identities are explicit arguments."""
    return {name: tool for name, tool in registry.items()
            if tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
            and _complete_target_contract(tool)}


def _complete_target_contract(tool):
    """A partial command contract cannot establish the complete action boundary.

    Opaque strings such as account-add tags and accept accounts are deliberately
    not decoded here. Every parameter must already have a typed contract role.
    Indirect selectors (for example a roster whose rows supply affected owners)
    and structured/unbounded filters need a richer contract before being offered.
    """
    contract = tool.contract
    schema = tool.definition.parameters
    if not contract or schema.get('additionalProperties') is not False:
        return False
    entities = dict(contract.entity_fields)
    targets = {entity_kind(kind) for kind in entities.values()} & TARGET_KINDS
    if len(targets) != 1:
        return False
    described = set(entities) | set(contract.filter_fields) | set(contract.channel_fields)
    properties = schema.get('properties', {})
    if set(properties) != described:
        return False
    # Required-field alternatives are supported; schema branches that introduce
    # or reinterpret parameters cannot inherit a partial top-level contract.
    if any(set(choice) - {'required'} for choice in schema.get('anyOf', ())):
        return False
    for field, parameter in properties.items():
        kind = entities.get(field)
        if kind is not None:
            identity = kind.removesuffix('_set')
            if identity not in TARGET_KINDS | {'discord_role', 'discord_channel'}:
                return False
            if kind.endswith('_set'):
                if parameter.get('type') != 'array':
                    return False
                parameter = parameter.get('items', {})
            expected = 'string' if identity == 'clash_account' else 'integer'
            if parameter.get('type') != expected:
                return False
        elif parameter.get('type') not in ('string', 'integer', 'number', 'boolean', 'null'):
            return False
    return True


def action_catalogue(registry):
    return json.dumps({'recommended_action_capabilities': [
        {'capability': name, 'description': tool.definition.description,
         'arguments': tool.definition.parameters,
         'targets': dict(tool.contract.entity_fields)}
        for name, tool in sorted(action_capabilities(registry).items())]}, ensure_ascii=False)


def _resolved_target(context, item, tool, accounts):
    arguments = item['arguments']
    target_fields = {field for field, kind in tool.contract.entity_fields
                     if entity_kind(kind) in TARGET_KINDS}
    if has_reference(arguments) or not target_fields <= arguments.keys():
        return False
    scope = compile_capability_call(tool, arguments)
    if member_target_issue(context, arguments, scope):
        return False
    targets = {}
    for entity in scope.get('entities', ()):
        kind = entity_kind(entity['kind'])
        if kind not in TARGET_KINDS:
            continue
        values = entity['key'] if isinstance(entity['key'], list) else [entity['key']]
        targets.setdefault(kind, set()).update(values)
    if (not targets.get('discord_member', set()) <= allowed_member_ids(context)
            or not targets.get('clash_account', set()) <= accounts.keys()):
        return False
    target = item['target']
    kind, key = (('discord_member', target['member_id']) if 'member_id' in target
                 else ('clash_account', target['player_tag']))
    # Count the entire affected set, including secondary member/account fields.
    # One known member plus one known account is still two affected targets.
    return targets.get(kind) == {key} and sum(len(values) for values in targets.values()) == 1


async def prepare_recommended_actions(flow, decision):
    """Offer optional changes without rewriting the finished assessment."""
    if (decision['intent_source'] == 'unresolved' or decision['open_choices']
            or not decision['recommendation'].strip() or not decision.get('recommended_actions')):
        return ()
    context = flow.context
    original_count = len(context.state.proposed_changes)
    eligible = action_capabilities(flow.recommendation_registry)
    registry = {**flow.registry, **eligible}
    refusals = []
    seen = {(action.capability_name, json.dumps(action.checked_arguments, sort_keys=True))
            for action in context.state.proposed_changes}
    for index, item in enumerate(decision.get('recommended_actions', ())):
        if len(context.state.proposed_changes) >= MAX_RECOMMENDED_ACTIONS:
            break
        name, arguments = item['capability'], item['arguments']
        signature = name, json.dumps(arguments, sort_keys=True)
        if name not in eligible or signature in seen:
            continue
        seen.add(signature)
        identity = 'assessment_action_' + str(index)
        while identity in flow.runner.completed_steps:
            identity += '_'
        step = {'id': identity, 'capability': name, 'arguments': arguments, 'depends_on': []}
        plan = {'goal': 'Prepare recommended changes', 'effort': 'low', 'output': 'text',
                'steps': [step]}
        checked = check_plan(plan, registry, completed_steps=flow.runner.completed_steps)
        if not checked.ok or checked.step_errors or not check_step(step, registry, resolved=True).ok:
            continue
        try:
            if not _resolved_target(context, item, eligible[name], flow.answer_accounts):
                continue
        except (TypeError, ValueError):
            continue
        existing = tuple(context.state.proposed_changes)
        if existing and (eligible[name].action_class is ActionClass.IRREVERSIBLE
                         or any(action.action_class is ActionClass.IRREVERSIBLE for action in existing)):
            if not all(action.action_class is eligible[name].action_class
                       and action.capability_name == name for action in existing):
                continue
        snapshot = tool_state_snapshot(context)
        # Specialist changes need no extra model round to open their group. They
        # still enter the same checked runner as a planned change.
        flow.registry[name] = eligible[name]
        result = (await flow.runner.run(plan, answer_complete=True))[identity]
        proposals = context.state.proposed_changes
        try:
            if len(proposals) > MAX_RECOMMENDED_ACTIONS:
                raise ValueError('Too many recommended changes')
            if len(proposals) > len(existing):
                check_bundle(tuple(proposals))
        except ValueError:
            restore_tool_state(context, snapshot)
            LOGGER.info('Assessment change exceeds preview bounds: capability=%s', name)
            continue
        if result.get('error') or result.get('flags', {}).get('status') in ('failed', 'refused'):
            restore_tool_state(context, snapshot)
            # Preserve the existing handler's refusal, with no new offer wording.
            if result.get('error') and result.get('cause') != 'invalid_arguments':
                refusals.append(result['error'])
        elif len(proposals) == len(existing):
            # Missing command inputs and no-op results are not action offers.
            restore_tool_state(context, snapshot)
    if len(context.state.proposed_changes) > original_count:
        await prepare_preview(context)
        context.state.preview_reply = preview_text(context.state.proposed_changes)
        context.state.preview_first = False
    return tuple(dict.fromkeys(refusals))
