"""Compose production answer operations for synchronous validation tests."""

from elbow_helper.agent.engine.account_lists import insert_resolved_lists, resolve_account_lists
from elbow_helper.agent.engine.assessment import _decision_parts, _render_decision


def insert_account_lists(parts, declarations, index):
    return insert_resolved_lists(parts, resolve_account_lists(declarations, index), index)


def decision_answer(content, discussion_message_ids, *, accounts=None, advice=True):
    accounts = accounts or {}
    result, parts, lists = _decision_parts(content, discussion_message_ids, accounts, advice)
    return _render_decision(result, parts, lists, accounts)
