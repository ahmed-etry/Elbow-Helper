"""Publication receipts are events; uncompleted publication prompts are state."""

import json

from elbow_helper.data.events import Event, initialize_state, load_state, record, save_state


WORKFLOW = "leadership_news"


def snapshot(connection):
    state = load_state(connection, WORKFLOW)
    receipts = dict(state["legacy_receipts"])
    for row in connection.execute(
            "SELECT case_key,detail_json FROM workflow_events WHERE workflow=? AND kind='news_published'",
            (WORKFLOW,)):
        receipts[row["case_key"]] = json.loads(row["detail_json"])
    return {"published": receipts, "prompts": state["prompts"]}


def startup(connection):
    initialize_state(connection, WORKFLOW, lambda: {"legacy_receipts": {}, "prompts": {}})
    return snapshot(connection)


def change(connection, edit):
    state = load_state(connection, WORKFLOW)
    edit(connection, state)
    save_state(connection, WORKFLOW, state)
    return snapshot(connection)


def published(connection, source, target, sent, *, actor_kind, actor_discord_id, via, occurred_at):
    subject = getattr(source.author, "id", None)
    record(connection, Event(
        WORKFLOW, str(source.id), "news_published", occurred_at,
        actor_kind=actor_kind, actor_discord_id=actor_discord_id, via=via,
        subject_discord_id=subject,
        source_channel_id=source.channel.id, source_message_id=source.id,
        detail={"channel_id": target.id, "message_id": sent.id, "guild_id": target.guild.id}))
