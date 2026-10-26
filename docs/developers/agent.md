# Agent

`elbow_helper.features.agent` handles questions and action requests sent by
mentioning the bot or replying to an agent conversation. It calls other
features through their query and command interfaces. Those features own their
saved data; shared infrastructure handles Clash and AI requests.

## Request handling

- `cog.py` receives messages and gathers nearby messages and conversation
  history. Requests from the same member or conversation run one at a time.
- `engine/service.py` uses a model judgment to separate social replies,
  community research and game advice. Clan rules, role duties and member
  account checks use research. Tactical game advice gets a generated decline;
  with a knowledge store configured, it first goes through research to check
  approved references. Short social replies can expand into research. A truncated
  short reply gets one complete rewrite within the same request limits.
- `plan/planning.py` reads the model's answer or proposed steps.
  `plan/checker.py` checks tool names, arguments, and references to earlier
  results. `PlanRunner` in `engine/steps.py` checks access and runs the steps.
- `AnswerFlow` in `engine/flow.py` checks the answer. `delivery.py` decides
  where it can be shown and presents changes for confirmation.

Request timeouts are set in `cog.py`; model usage and result size limits are
in `engine/budgets.py`. When investigating an unfinished request, check the
`Agent usage` log for its status and usage.

`engine/member_usage.py` keeps rolling allowances in memory: 30 banter
requests per hour, and 10 questions per hour and 40 per day, per member and
server. Expanding banter into research counts as a question instead.
Lead Plus, Core and sleeping Co-Leaders and Cross-Leaders are exempt.
`AnswerFlow._limit_plan` reserves deep analysis (assessment mode and `max`
effort) for those same members, including on revised and corrected plans.

The meme catalogue is `discord_actions/gif_library.json`; local media lives
in `data/agent/gifs/`. In `engine/social_reply.py`, the model shortlists three
items with fit scores. Ordinary banter sends at fit 4 or higher, or 5 if the
bot's previous channel reply was a meme. `discord_actions/gif_library.py`
avoids clips already seen in the channel context, using an unused clip of
the same joke when possible. Explicit meme requests bypass the fit threshold
and may repeat a clip. `discord_actions/meme_usage.py` records delivered memes
across the server and prefers fresh jokes.

## Adding tools and actions

Research starts with about 20 abilities marked `default_visible` in
`engine/registry.py`. The rest are opened through `families/catalogue.py`
discovery, which installs tool definitions without running them or granting
access. `engine/self_knowledge.py` supplies the complete registered list,
including hidden abilities and their permission requirements, when the bot
answers questions or makes claims about its own abilities.

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
`access.py` checks each source against the destination scope from
`disclosure.py` before reading it. Role-classified sources use the member's
reader roles; other sources require the member's channel permissions and,
for private threads, membership or Manage Threads. The bot must be able to
read every source. These checks apply again when using saved history or
reports and before sending an answer. Excluded evidence stays out of the
shared answer; the private check is described under Community knowledge. See
[Privacy and AI](../users/privacy-and-ai.md#agent) for the sharing controls and
the audit-log restriction.

## Answer checks

`engine/grounding.py` replaces `[[message:ID]]` with the recorded author,
message text, and source link. It rejects unknown or conflicting message
references; it does not judge the meaning of prose by matching English words.
The answer model must distinguish sourced statements from its recommendations,
using message authors, chronology and explicit reply targets. A correct citation
alone does not establish that an interpretation is faithful.
When the family catalogue is not active, `factual_answers.py` checks explicit
clan member counts, count-only query results and battle-log figures against
typed results for the same subject and metric. A conflict gets one answer-only
repair with the verified value. A repeated conflict or unavailable repair keeps
the prose and appends the verified figure. Single-clan member counts also use
checked model plans. Filtered counts and list requests go through an answer
round, including requests for names and explanations.

An unresolved message reference gets one answer-only repair. If repair cannot
finish, the reply drops the unresolved citation and keeps the prose and valid
citations. Claims explicitly attributed to a missing message receive a short
qualifier. Visible local messages are also available as citation evidence, and
oversized results retain whole records rather than raw JSON excerpts.
Valid measured figures and completed command outcomes survive failures in
independent results. Access checks still apply before delivery, including
recovery replies.

`AnswerFlow._advance_answer` normally uses low effort for writing and grounding
repairs, independently of planning effort. Assessment composition uses max
effort, further read rounds use high, and later repairs use none. Private
passes follow the same policy.

Unit tests check source integrity, numeric results and recovery. To assess
interpretation with the actual answer model, run
`python -m tests.features.agent.engine.evaluate_grounding` where the bot's hosting
settings are available. It uses synthetic evidence and production answer
instructions, prints answers with review criteria, and never connects to Discord.
Review the output: finishing the command is not a semantic pass. Use `--case feedback`
to run only the feedback example. This evaluates answer writing, not live tool
selection or Discord delivery.

## Conversation state

`conversation/` saves questions, answers, lookup results, report references,
source permissions, and delivery progress. Reply IDs connect follow-up
messages to the conversation after a restart. Delivery progress lets the bot
check an uncertain send and reuse the answer.

Working conversations and Discord research jobs expire after six idle hours.
Context compilation keeps the most recent authorized whole turns that fit,
including intervening constraints and corrections, without filtering by wording.
Token estimates and soft context targets are calibrated against archived usage;
see [Context measurements](agent-context-measurements.md) for the method and sizes.
Long conversations can lose older turns sooner to keep their saved size
within limits. Action execution and scheduled requests have separate saved
state. See [Saved data](../reference/storage.md) for the files and
[Privacy and AI](../users/privacy-and-ai.md#agent) for how long request text and
troubleshooting records are kept.

## Community knowledge

`KnowledgeStore` supplies approved reference sections within the destination's
scope. Research starts with a visible title and filename index, without
preloading section bodies, including those marked `always_loaded`.

Use `titles` to open a clearly matching topic from the index, or a word search
when no title clearly matches. Searches return bodies and titles only within
the active scope. Relevant matches outside that scope may set
`available_privately` when the asker can read them, without returning their
titles, section IDs or bodies. Legacy `scope` arguments cannot widen access.

`knowledge/answers.py` accepts `{"needs_private_evidence":true,"answer":"..."}`
to keep the supported channel answer and offer **Show full answer** when
relevant excluded evidence is available to the asker. The old
`needs_restricted_knowledge` key is rejected. No excluded evidence is opened
until the asker clicks the button in `actions/answer_view.py`.
`private_answer_pass` then starts fresh tools, evidence and a model session
at the asker's current clearance, retaining visible causal conversation
history. It can run reads and create artifacts, but cannot make changes.

The private result is shown only to the asker. **Post here** can share that
result after access is rechecked; it does not authorize later answers.
Audit results remain private. Retained evidence, generated artifacts and
change details keep their source restrictions. Knowledge reads use section
pages and continuation offsets within the request's evidence limits.

The `Agent evidence scope` log records the request ID, destination clearance,
active scope, excluded sources and whether a private check was offered.
See [Agent knowledge files](../operators/agent-knowledge.md) for editing and
reload instructions.

## Testing

Tests live under `tests/features/agent`, grouped like the feature code.
For tool changes, check invalid arguments, failed earlier steps, lost access,
source tracking, incomplete results, and confirmation checks as relevant.
Model-client tests live in `tests/core/test_ai_infrastructure.py`. See
[Testing and verification](testing.md) for commands and manual checks.
