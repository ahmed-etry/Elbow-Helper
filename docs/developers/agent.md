# Agent

`elbow_helper.features.agent` handles questions and action requests sent by
mentioning the bot or replying to an agent conversation. It calls other
features through their query and command interfaces. Those features own their
saved data; shared infrastructure handles Clash and AI requests.

## Request handling

- `cog.py` receives messages and gathers nearby messages and conversation
  history. Requests from the same member or conversation run one at a time.
- `engine/service.py` prepares the model request and available tools. Simple
  lookups start with fewer tools and can expand to include the full tool list,
  data guide, and community knowledge.
- `plan/planning.py` reads the model's answer or proposed steps.
  `plan/checker.py` checks tool names, arguments, and references to earlier
  results. `PlanRunner` in `engine/steps.py` checks access and runs the steps.
- `AnswerFlow` in `engine/flow.py` checks the answer. `delivery.py` decides
  where it can be shown and presents changes for confirmation.

Request timeouts are set in `cog.py`; model usage and result size limits are
in `engine/budgets.py`. When investigating an unfinished request, check the
`Agent usage` log for its status and usage.

## Adding tools and actions

`RegisteredAgentTool` connects a tool's name and arguments to its handler.
Its `CapabilityContract` describes the data it can read, the source channels,
and the roles a member needs. Channel reads must record their sources so
access can be checked again when results or saved reports are used.

Changes use `PreparedAction` with a preview and a check that the change is
still valid. Before running a change, the action code checks permissions and
whether the preview still matches. Keep feature command adapters under
`capabilities/`; `commands/bridge.py` builds tools from registered command
definitions and calls the feature adapters.

## Access and sharing

Core leadership and members with the Agent Tester role can use the agent.
`access.py` checks their roles and channel permissions for each source. Both
the member and the bot must be able to read a source channel; the member must
also belong to any private thread being read. These checks apply again when
using saved history or reports and before sending an answer.

`disclosure.py` checks whether everyone who can see the destination can read
the answer's sources. When that cannot be established, `PrivateAnswerView`
lets the requesting member view the answer privately. See
[Privacy and AI](../users/privacy-and-ai.md#agent) for the sharing controls and
the audit-log restriction.

## Answer checks

`engine/grounding.py` replaces `[[message:ID]]` with the recorded author,
message text, and source link. It rejects unknown message IDs and checks
specific claims against the information read during the request.
`factual_answers.py` and `strict_answers.py` build supported answers, such as
counts and tag lists, directly from lookup results.

When adding an answer format, test missing or conflicting information as well
as a valid result. The checks depend on the claim or lookup being supported.

## Conversation state

`conversation/` saves questions, answers, lookup results, report references,
source permissions, and delivery progress. Reply IDs connect follow-up
messages to the conversation after a restart. Delivery progress lets the bot
check an uncertain send and reuse the answer.

Working conversations and Discord research jobs expire after six idle hours.
Long conversations can lose older turns sooner to keep their saved size
within limits. Action execution and scheduled requests have separate saved
state. See [Saved data](../reference/storage.md) for the files and
[Privacy and AI](../users/privacy-and-ai.md#agent) for how long request text and
troubleshooting records are kept.

## Community knowledge

`KnowledgeStore` supplies approved reference sections that the member can
read. Short sections marked `always_loaded` are included in research requests.
The topic list includes public titles and filenames; members can search
restricted sections when their roles allow it.

`search_approved_knowledge` searches words, opens exact titles, and continues
long sections. Follow its continuation fields when the full text is needed.
See [Agent knowledge files](../operators/agent-knowledge.md) for editing and
reload instructions.

## Testing

Tests live under `tests/features/agent`, grouped like the feature code.
For tool changes, check invalid arguments, failed earlier steps, lost access,
source tracking, incomplete results, and confirmation checks as relevant.
Model-client tests live in `tests/core/test_ai_infrastructure.py`. See
[Testing and verification](testing.md) for commands and manual checks.
