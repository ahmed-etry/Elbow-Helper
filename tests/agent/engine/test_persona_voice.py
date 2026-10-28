"""Plain banter and a shared media mood reach the model without chat matching."""

import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.routing import SOCIAL_INSTRUCTION
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.social_reply import (
    SOCIAL_REPLY_INSTRUCTION, social_reply_definition,
)
from elbow_helper.agent.plan.checker import valid_arguments
from elbow_helper.agent.plan.format import system_instructions
from agent.engine.helpers import social_step
from agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


class PersonaVoiceInstructionTests(unittest.TestCase):
    def assert_plain_voice(self, prompt):
        for rule in (
            "Talk like a blunt regular in clan chat: short, plain, rude when it fits",
            "Swearing is normal speech",
            "When someone's being dumb, you can call it out in passing and move on",
            "Skip stock comparison formulas",
            '"the X equivalent of Y", stacked jokes and punchlines that need explaining',
            "a short reaction or agreement is enough",
            "never require an extra twist",
            "Do not retell the message, explain its joke or recycle a nearby punchline",
            "celebrate the win directly; do not undercut it",
            "If the stated mechanic is impossible, call out the mismatch briefly instead of endorsing it",
            "Check the mechanic before joining a numeric premise",
            "Do not invent game mechanics to support a joke",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)
        for forced_joke in (
            "commit and add one twist", "add a twist, escalation",
            "Bare one-word agreement fits only", "Add a new angle instead",
        ):
            self.assertNotIn(forced_joke, prompt)

    def test_plain_voice_and_game_sense_reach_both_permission_modes(self):
        for actions in (False, True):
            with self.subTest(actions=actions):
                self.assert_plain_voice(system_instructions({}, actions_enabled=actions, social=True))

    def test_social_route_does_not_reintroduce_a_required_punchline(self):
        self.assertIn("A short reaction or agreement is enough; never force an extra twist", SOCIAL_INSTRUCTION)
        self.assertIn("one short, plain line with one idea", SOCIAL_INSTRUCTION)
        self.assertNotIn("add a twist, escalation", SOCIAL_INSTRUCTION)

    def test_mood_agreement_is_part_of_the_shared_and_social_instructions(self):
        for prompt in (system_instructions({}, actions_enabled=False, social=True),
                       system_instructions({}, actions_enabled=True, social=True), SOCIAL_REPLY_INSTRUCTION):
            with self.subTest(prompt=prompt):
                self.assertIn("A celebration reaction requires a celebratory line", prompt)
                self.assertIn("never pair it with a jab that undercuts the win", prompt)

    def test_schema_binds_line_reactions_and_both_image_choices_to_one_mood(self):
        for registry, combo_due in (({}, False), ({"react_to_request": object()}, False),
                                    ({"react_to_request": object()}, True)):
            with self.subTest(reactions=bool(registry), combo_due=combo_due):
                definition = social_reply_definition(registry, combo_due=combo_due)
                schema = definition.parameters
                fields = schema["properties"]
                self.assertIn("A meme that lands is delivered alone; text is its standalone fallback", definition.description)
                self.assertIn("Your response to the exchange. Never open by repeating", fields["text"]["description"])
                self.assertIn("Share the mood of reactions and any GIF", fields["text"]["description"])
                self.assertIn("All share the text's mood", fields["reactions"]["description"])
                self.assertIn("A celebration reaction requires a celebratory line, never a jab",
                              fields["reactions"]["description"])
                self.assertIn("The three library memes closest to this moment", fields["meme_shortlist"]["description"])
                self.assertIn("shortlist the three library memes closest to this moment", SOCIAL_REPLY_INSTRUCTION)
                self.assertIn("sarcastic or ironic uses listed for a meme count", SOCIAL_REPLY_INSTRUCTION)
                self.assertIn("rate how well each works as the reply on its own", SOCIAL_REPLY_INSTRUCTION)
                self.assertEqual(schema["required"], ["mechanic_check", "reaction_fit", "reaction_basis",
                    "reply_form", "gif_requested", "meme_shortlist", "text", "reactions"])
                self.assertFalse(schema["additionalProperties"])
                choice = {"mechanic_check": "no_gameplay_number", "reply_form": "text", "text": "gg",
                          "reaction_fit": False, "reaction_basis": "",
                          "meme_shortlist": [{"id": item, "fit": 1} for item in
                              ("ts_lowkey_tuff", "things_will_happen_horse", "son_not_tuff")],
                          "gif_requested": False, "reactions": []}
                self.assertTrue(valid_arguments(choice, schema))
                self.assertFalse(valid_arguments({key: value for key, value in choice.items()
                                                  if key != "text"}, schema))


class PersonaVoiceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_social_pairs_receive_the_contract_and_deliver_in_one_round(self):
        for question, line, emoji in (
            ("We won the practice match, celebrate it.", "clean finish, gg", "\U0001f389"),
            ("That practice hit fell short, commiserate.", "rough finish", "\U0001f622"),
            ("The imaginary nap trophy is mine. Agree briefly.", "all yours", None),
        ):
            with self.subTest(question=question):
                context = _context()
                context.source_message.id = 300
                context.source_message.add_reaction = AsyncMock()
                session = _AgentSession([social_step(
                    line, form="text_reactions" if emoji else "text", meme_id=None,
                    reactions=[emoji] if emoji else [],
                )])
                model = _AgentModel(session, route="social")
                answer = await AgentService(model).answer(question=question, local_context="", context=context)
                self.assertEqual(answer, line)
                self.assertEqual(len(session.calls), 1)
                PersonaVoiceInstructionTests().assert_plain_voice(model.request["system_prompt"])
                self.assertIn(SOCIAL_REPLY_INSTRUCTION, model.request["system_prompt"])
                schema = model.request["tools"][0].parameters
                self.assertIn("All share the text's mood", schema["properties"]["reactions"]["description"])
                if emoji:
                    context.source_message.add_reaction.assert_awaited_once_with(emoji)
                else:
                    context.source_message.add_reaction.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
