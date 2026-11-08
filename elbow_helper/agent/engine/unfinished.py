"""Safe notice when no model-authored answer can be completed."""

from elbow_helper.agent.conversation.wording import AGENT_ANSWER_UNFINISHED


def unfinished_answer(question, evidence):
    """Evidence belongs in the answer model, never in a fallback prose renderer."""
    return AGENT_ANSWER_UNFINISHED
