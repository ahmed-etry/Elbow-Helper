# Agent

`elbow_helper.agent` handles bot mentions, conversation replies and approved saved requests.

## Requests

`loop/` runs one conversation with the LLM. The LLM can call read tools and use their results in its next turn. It can also use a banter reply tool for text, reactions or a GIF. Approved community notes are listed in a section index so the LLM can open the sections it needs. [Agent knowledge files](../operators/agent-knowledge.md) explains how to edit those notes.

Reads use the family database through `data/reads` and reuse feature services through `agent/features/`. Each tool declares its inputs and access requirements. Record which channels and permissions supplied a result so the bot can check access again before reusing it or sending an answer.

## Changes and private answers

`actions/` prepares a preview for changes and checks permissions and the target again when the requester presses **Confirm**. `scheduled/` runs approved reminders, saved requests and watches.

`safety/` checks what the requester, bot and destination channel can see. **Show full answer** starts a fresh private pass with the requester's access. **Post here** checks access again before sharing that result.

`conversation/` records questions, replies and lookup results that later turns can use. See [Privacy and AI](../users/privacy-and-ai.md#agent) for retention and [Testing](testing.md) for offline checks.
