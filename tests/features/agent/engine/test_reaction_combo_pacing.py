"""Script combo offers, failures and channel pacing without live services."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.discord_actions.reaction_pacing import channel_combo_history, reply_combo_due, combo_slot_status
from elbow_helper.features.agent.discord_actions.reactions import react_to_request
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.social_reply import salvage_choice, social_reply_definition
from elbow_helper.features.agent.models import ChannelReactions
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class ReactionComboPacingTests(unittest.IsolatedAsyncioTestCase):
    def context(self, *, history=(), channel=None, identifier=300):
        context = _context()
        context.source_message.id = identifier
        context.source_message.add_reaction = AsyncMock()
        context.state.channel_bot_combos = history
        context.state.channel_reactions = channel if channel is not None else ChannelReactions()
        context.state.reaction_combo_revision = context.state.channel_reactions.combo_revision
        return context

    def row(self, identifier, *, author=42, emojis=(), others=()):
        return SimpleNamespace(id=identifier, author=SimpleNamespace(id=author), reactions=[
            *(SimpleNamespace(emoji=value, me=True) for value in emojis),
            *(SimpleNamespace(emoji=value, me=False) for value in others),
        ])

    def test_schema_offers_combo_only_in_a_slot_and_salvage_cannot_smuggle_more_emojis(self):
        registry = {"react_to_request": object()}
        for due in (False, True):
            schema = social_reply_definition(registry, combo_due=due).parameters["properties"]
            self.assertEqual("text_reaction_combo" in schema["reply_form"]["enum"], due)
            self.assertEqual(schema["reactions"]["maxItems"], 3 if due else 1)
            for form in ("text_reactions", "text_reaction_combo", "invalid"):
                choice = salvage_choice({"mechanic_check": "no_gameplay_number", "reply_form": form, "text": "Synthetic line.",
                                         "reaction_fit": True, "reaction_basis": "Synthetic semantic connection.",
                                         "meme_shortlist": [{"id": item, "fit": 1} for item in
                                             ("ts_lowkey_tuff", "things_will_happen_horse", "son_not_tuff")],
                                         "gif_requested": False,
                                         "reactions": ["\U0001f914", "\U0001f4a1", "\U0001f680"]},
                                        registry, combo_due=due)
                self.assertEqual(len(choice["reactions"]), 3 if due and form == "text_reaction_combo" else 1)
        self.assertNotIn("text_reaction_combo", social_reply_definition({}, combo_due=True).parameters[
            "properties"]["reply_form"]["enum"])

    def test_metadata_snapshot_ignores_other_users_and_holds_a_pending_combo(self):
        rows = [self.row(1, emojis=("\U0001f914", "\U0001f4a1")), self.row(2, author=999),
                self.row(3, others=("\U0001f680", "\U0001f389")), self.row(4, author=999)]
        self.assertEqual(channel_combo_history(rows, 999), (True,))
        self.assertIsNone(channel_combo_history(rows, None))
        self.assertEqual(channel_combo_history(rows[-2:], 999), ())
        self.assertEqual(channel_combo_history(rows[:1], 999), (True,))
        self.assertEqual(channel_combo_history(rows, 999, last_combo_message_id=5), (True,))
        self.assertEqual(channel_combo_history([self.row(10), self.row(11, author=999)], 999,
                                              last_combo_message_id=10), (True,))

    def test_malformed_optional_media_does_not_discard_a_valid_combination(self):
        choice = {'mechanic_check': 'no_gameplay_number', 'reply_form': 'text_reaction_combo',
                  'text': 'Synthetic line.', 'reaction_fit': True,
                  'reaction_basis': 'The pair expresses mock disbelief.',
                  'meme_shortlist': [{'id': 'ts_lowkey_tuff', 'fit': 'bad'}, 42],
                  'gif_requested': False, 'reactions': ['\U0001f914', '\U0001f440']}
        registry = {'react_to_request': object()}
        result = salvage_choice(choice, registry, combo_due=True)
        self.assertEqual(result['reactions'], choice['reactions'])
        self.assertEqual(result['reply_form'], 'text_reaction_combo')
        self.assertEqual(salvage_choice(choice, registry, combo_due=False)['reactions'], ['\U0001f914'])
        self.assertFalse(result['gif_requested'])

    async def test_19_fresh_conversations_share_channel_slots_and_always_keep_a_line(self):
        rows, combos = [], []
        channel = ChannelReactions()
        pairs = [("\U0001f914", "\U0001f4a1"), ("\U0001f680", "\U0001f389")]
        for position in range(1, 20):
            context = self.context(identifier=position * 10, channel=channel,
                history=channel_combo_history(rows, 999, last_combo_message_id=channel.combo_message_id))
            due = reply_combo_due(context)
            emojis = pairs[len(combos) % 2] if due else ("\U0001f440",)
            session = _AgentSession([social_step("Synthetic line.", meme_id=None,
                form="text_reaction_combo" if due else "text_reactions", reactions=emojis)])
            model = _AgentModel(session, route="social")
            answer = await AgentService(model).answer(question="Synthetic exchange", local_context="", context=context)
            self.assertEqual(answer, "Synthetic line.")
            self.assertEqual(len(session.calls), 1)
            schema = model.request["tools"][0].parameters["properties"]
            self.assertEqual("text_reaction_combo" in schema["reply_form"]["enum"], due)
            self.assertIn('Reaction combinations are available as an option' if due else 'Reaction combinations are unavailable',
                          model.request['prompt'])
            self.assertEqual(context.source_message.add_reaction.await_count, 2 if due else 1)
            if due:
                combos.append(position)
            rows.extend([self.row(position * 10, emojis=context.state.reactions.emojis),
                         self.row(position * 10 + 1, author=999)])
        self.assertEqual(combos, list(range(1, 20, 2)))

    async def test_plain_replies_do_not_increase_the_combo_share_of_reacted_replies(self):
        rows, reacted, combos = [], 0, 0
        channel = ChannelReactions()
        for position in range(1, 37):
            context = self.context(identifier=position * 10, channel=channel,
                history=channel_combo_history(rows, 999, last_combo_message_id=channel.combo_message_id))
            due = reply_combo_due(context)
            emojis = () if position % 3 == 0 else ("\U0001f602", "\U0001f480") if due else ("\U0001f440",)
            form = 'text_reaction_combo' if len(emojis) == 2 else 'text_reactions' if emojis else 'text'
            session = _AgentSession([social_step('That went well.', meme_id=None,
                form=form, reactions=emojis)])
            await AgentService(_AgentModel(session, route='social')).answer(
                question='well that went sideways', local_context='', context=context)
            delivered = context.state.reactions.emojis
            reacted += bool(delivered)
            combos += len(delivered) == 2
            rows.extend([self.row(position * 10, emojis=delivered), self.row(position * 10 + 1, author=999)])
        self.assertEqual((reacted, combos), (24, 12))

    async def test_closed_unknown_and_research_slots_cannot_deliver_a_combo(self):
        for history, route in (((True,), "social"), (None, "social"), ((), "research")):
            context = self.context(history=history)
            if route == "social":
                session = _AgentSession([social_step("Synthetic line.", meme_id=None,
                    form="text_reaction_combo", reactions=["\U0001f914", "\U0001f4a1"])])
                model = _AgentModel(session, route=route)
                answer = await AgentService(model).answer(question="Synthetic exchange", local_context="", context=context)
                self.assertEqual(answer, "Synthetic line.")
                self.assertNotIn("text_reaction_combo", model.request["tools"][0].parameters["properties"]["reply_form"]["enum"])
                self.assertLessEqual(context.source_message.add_reaction.await_count, 1)
            result = await react_to_request(context, {"emoji": ["\U0001f680", "\U0001f389"]})
            self.assertEqual(result["cause"], "combo_not_offered")

    async def test_failed_or_declined_combo_leaves_the_next_slot_available(self):
        for declined in (False, True):
            context = self.context()
            if not declined:
                context.source_message.add_reaction.side_effect = OSError("Synthetic failure")
            session = _AgentSession([social_step("Synthetic fallback.", meme_id=None,
                form="text" if declined else "text_reaction_combo",
                reactions=[] if declined else ["\U0001f914", "\U0001f4a1"])])
            answer = await AgentService(_AgentModel(session, route="social")).answer(
                question="Synthetic exchange", local_context="", context=context,
            )
            self.assertEqual(answer, "Synthetic fallback.")
            self.assertEqual(context.state.channel_reactions.combo_message_id, 0)
            self.assertTrue(reply_combo_due(context))

    async def test_concurrent_offers_share_one_slot_and_failures_do_not_advance_pacing(self):
        channel = ChannelReactions()
        contexts = [self.context(channel=channel, identifier=identifier) for identifier in (300, 301)]
        for context in contexts:
            context.state.reaction_combo_due = True
        results = await asyncio.gather(*(
            react_to_request(context, {"emoji": ["\U0001f914", "\U0001f4a1"]}) for context in contexts
        ))
        self.assertEqual(sum(result["reacted"] for result in results), 1)
        self.assertEqual(sum(context.source_message.add_reaction.await_count for context in contexts), 2)
        self.assertEqual(channel.combo_revision, 1)
        self.assertEqual(channel.combo_message_id, 300)

    async def test_single_calls_cannot_build_an_unoffered_combo_and_duplicates_do_not_consume_a_slot(self):
        context = self.context()
        await react_to_request(context, {"emoji": "\U0001f914"})
        self.assertEqual((await react_to_request(context, {"emoji": "\U0001f4a1"}))["cause"], "combo_not_offered")
        context = self.context()
        context.state.reaction_combo_due = True
        result = await react_to_request(context, {"emoji": ["\U0001f914", "\U0001f914"]})
        self.assertEqual(result["flags"]["status"], "partial")
        self.assertEqual(context.state.channel_reactions.combo_message_id, 0)
        self.assertEqual(context.source_message.add_reaction.await_count, 1)

    async def test_a_combo_sent_after_the_snapshot_closes_the_stale_offer(self):
        channel = ChannelReactions()
        stale = self.context(channel=channel, identifier=301)
        first = self.context(channel=channel)
        first.state.reaction_combo_due = True
        await react_to_request(first, {"emoji": ["\U0001f914", "\U0001f4a1"]})
        session = _AgentSession([social_step("Synthetic line.", meme_id=None)])
        model = _AgentModel(session, route="social")
        await AgentService(model).answer(question="Synthetic exchange", local_context="", context=stale)
        self.assertNotIn("text_reaction_combo", model.request["tools"][0].parameters["properties"]["reply_form"]["enum"])
        self.assertEqual(combo_slot_status(stale), 'revision_changed')

    def test_gate_reasons_distinguish_absent_metadata_from_false_and_cooldown(self):
        context = self.context(history=())
        self.assertEqual(combo_slot_status(context), 'available')
        context.state.channel_bot_combos = (True,)
        self.assertEqual(combo_slot_status(context), 'cooldown')
        context.state.channel_bot_combos = None
        self.assertEqual(combo_slot_status(context), 'reply_history_unavailable')
        context.state.channel_reactions.emojis = None
        self.assertEqual(combo_slot_status(context), 'reaction_history_unavailable')
