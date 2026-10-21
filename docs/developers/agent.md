# Agent

`elbow_helper.features.agent` handles questions and action requests sent by
mentioning the bot or replying to an agent conversation. It calls other
features through their query and command interfaces. Those features own their
saved data; shared infrastructure handles Clash and AI requests.

## Request handling

- `cog.py` receives messages and gathers nearby messages and conversation
  history. Requests from the same member or conversation run one at a time.
- `engine/service.py` starts non-social requests with the full tool list,
  data guide, and channel-readable community knowledge in a stable system
  prefix. Short social replies use a smaller route that can expand. A bare,
  fully matched single-clan member count can use one read without a model.
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
message text, and source link. It rejects unknown or conflicting message
references; it does not judge the meaning of prose by matching English words.
The answer model must distinguish sourced statements from its recommendations,
using message authors, chronology and explicit reply targets. A correct citation
alone does not establish that an interpretation is faithful.
`factual_answers.py` and `strict_answers.py` build supported answers, such as
counts and tag lists, directly from lookup results.

An unresolved message reference gets one answer-only repair. If repair cannot
finish, the reply retains referenced source messages that were actually read
and identifies the missing result. Valid measured figures and completed command
outcomes survive failures in independent results. Access checks still apply
before delivery, including recovery replies.

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

`KnowledgeStore` supplies approved reference sections that the asker can
read. Research requests load short `always_loaded` sections only when the
channel audience may read them. The topic list contains public titles and
filenames.

Use `titles` to open a clearly matching topic from the index, or a word search
when no title clearly matches. Searches cover the asker's access groups.
The first answer pass receives channel-readable bodies and a title, section ID
and `available_privately` note for other matches. Sections outside the asker's
access are absent, including their titles. Legacy `scope` arguments do not
release private bodies.

When channel-readable material suffices, the answer stays public. The model can
request needed bodies with `{"needs_restricted_knowledge":true,"section_ids":["..."]}`.
One additional answer pass receives only those sections and cannot run tools.
The older boolean-only signal opens a single withheld section directly; ambiguous
or invalid selections get one answer-only correction before any bodies are opened.
Malformed internal signals, including extra fields or wrong value types, are
recognized as protocol rather than delivered as answer text. Only a valid
selection can open bodies; an invalid correction stops with the existing failure reply.
Unused private matches do not trigger another answer pass.

Access is rechecked before using bodies or titles and before delivery. If the
destination audience cannot read the evidence, the answer uses the private-result
controls. An explicit **Post here** decision can cover later answers for the same
requester and channel, with access rechecked. Knowledge section IDs include their
contents, so a different or revised section needs its own decision. Audit results
remain private. Retained evidence, generated artifacts and change details also
keep their source restrictions.

The private pass receives complete selected sections within the existing
evidence limits. If they do not fit, the request stops with the existing
shorter-request response rather than answering from clipped sections.

For a private answer, check the `Agent audience decision` log for the request ID.
`failed_requirement` names the rejected access group or source permission;
`reader_kind` distinguishes a role group, a bot, or an unknown member, without
member names. An incomplete-cache reason means the requirement could not be
proved from channel permissions, rather than identifying an unauthorized person.
See [Agent knowledge files](../operators/agent-knowledge.md) for editing and
reload instructions.

## Testing

Tests live under `tests/features/agent`, grouped like the feature code.
For tool changes, check invalid arguments, failed earlier steps, lost access,
source tracking, incomplete results, and confirmation checks as relevant.
Model-client tests live in `tests/core/test_ai_infrastructure.py`. See
[Testing and verification](testing.md) for commands and manual checks.
