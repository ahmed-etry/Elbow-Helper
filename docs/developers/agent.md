# Agent

`elbow_helper.agent` handles bot mentions and replies in agent conversations.
It reads the family database through `data/reads` and uses feature services
for anything a feature handles. Features never import the agent.

## Request handling

`cog.py` gathers the request, nearby messages, and earlier parts of the
conversation. Requests from the same member or conversation run one at a time.
`engine/service.py` has the LLM choose between a chat reply, looking things
up, or declining a request for attack or build advice. A chat reply can turn
into a lookup.

`plan/planning.py` reads the steps the LLM proposes. `plan/checker.py` checks
each tool name, its arguments, and any use of an earlier step's result.
`engine/steps.py` runs the checked steps, `engine/flow.py` decides on further
lookups and checks the answer, and `conversation/delivery.py` sends the answer
or shows a change for the member to approve.

Time limits for a request are in `cog.py`; limits on LLM use and result sizes
are in `engine/budgets.py`. The `Agent usage` log line records how each
request ended and what it used. `conversation/context.py` keeps whole earlier
turns the member may see, up to its size limit. It estimates tokens to choose
what fits; the provider's usage figures give the real counts. The fixed
instructions and tool list come before the request's reference files and
lookup results in what the LLM receives.

## Tools and changes

`engine/registry.py` builds the tool list. The everyday lookups in `reads/` are
offered from the start. `engine/catalogue.py` lets the LLM ask for more groups
of tools; that only shows them, without running them or giving access.
`engine/self_knowledge.py` tells the agent what it can do and which roles each
ability needs.

Each `RegisteredAgentTool` defines its arguments and a handler. Its
`CapabilityContract` says what data it reads, which channels its results come
from, and which roles it needs. Record where each result came from when you
read it, so access can be checked again when results, files, or saved reports
are used later.

The agent's links to feature commands live in `agent/features/`.
`commands/bridge.py` connects their command definitions to feature services.
Reuse those services rather than copying feature rules. `reads/sql/` holds the
list of tables the agent may query and checks each SQL read.

Changes use `PreparedAction`: build a preview, then check permissions and the
thing being changed again before running it. `actions/` handles the Confirm
step, the record of what ran, private result buttons, and undo. `scheduled/`
runs the reminders, saved requests, and watches that members approved.

## Access and answer checks

Lead Plus, Core leadership, and Agent Testers can use the agent.
`safety/access.py` checks that both the member and the bot can see each
source. `safety/disclosure.py` keeps out anything the channel receiving the
answer can't see. Sources tied to roles use those roles; other channels use
Discord permissions, and private threads need membership or Manage Threads.
These checks run again before results are reused or an answer is sent.
Audit-log answers stay private.

`engine/grounding.py` matches each message the answer cites to a source the
agent read, and rejects unknown or conflicting ones. `engine/flow.py` has the
LLM fix a rejected answer within the request's limits.

## Conversations and community references

`conversation/` saves questions, answers, what the agent found, the access
used, reply IDs, and how far sending got. Conversations and longer Discord
searches are dropped after six hours with no new messages. The saved request
and reply text is kept separately; see
[Privacy and AI](../users/privacy-and-ai.md#agent) for how long and
[Saved data](../reference/storage.md) for where.

`knowledge/` reads the approved Markdown reference files. For each question
the agent first sees the headings and filenames it may read, then opens the
sections it needs, including those marked `always_loaded`. When a section the
channel can't read matches, the agent can say more is available privately,
without seeing its title or contents.

When that section could change the answer, **Show full answer** runs a second,
private pass after the asker clicks. That pass uses the asker's access and can
read data and create files, but can't make changes. **Post here** checks access
again, then shares that result; later answers still can't use the private
section. See [Agent knowledge files](../operators/agent-knowledge.md) for
editing and reloading the files.

## Tests

Tests live under `tests/agent`, grouped like the agent code. Changes to a tool
need tests, where they apply, for bad arguments, a failed earlier step, lost
access, recording where results came from, partial results, and the Confirm
step. LLM client tests live in `tests/core/test_ai_infrastructure.py`. See
[Testing](testing.md).
