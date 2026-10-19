# Agent

`elbow_helper.features.agent` owns mention-driven requests, checked tool plans,
conversation state, and confirmed actions. Feature adapters use the owning
features' query and command interfaces. They do not take ownership of those
features' databases or Clash transport.

## Request flow

`AgentCog` accepts mentions and replies to retained agent conversations in the
configured guild. Entry is limited to Core leadership and the Agent Tester
role. This rollout check does not grant access to restricted tools or sources.

Requests take a conversation lock, a member lock, and one of four execution
slots. Queue waiting is limited to 120 seconds; execution has a separate
480-second deadline. The cog gathers nearby messages and authorized conversation
history; `AgentService` loads request images before building the model context.

1. `engine/service.py` builds the capability registry and model context. Simple
   lookups start with a smaller catalogue and can expand to the research route.
   The research prompt includes the data guide and community knowledge.
2. `plan/planning.py` accepts a direct answer or a structured plan. Plans name
   a goal, effort, output, and steps with arguments and dependencies.
3. `plan/checker.py` checks plan structure, registered names, schemas, result
   references, and action boundaries. `PlanRunner` checks resolved arguments
   and access before execution. Independent reads may run concurrently;
   dependent steps wait for their inputs.
4. Results return to the model for an answer or revised plan. Failed steps keep
   their errors; dependent steps cannot treat them as successful results.
5. `AnswerFlow` checks the final answer, then delivery checks its audience.
   Change proposals go through the confirmation flow described below.

`engine/budgets.py` bounds cumulative provider use, including recovery rounds:

| Route | Model rounds | Input tokens | Output tokens | Output tokens per round |
|---|---:|---:|---:|---:|
| Lookup | 4 | 80,000 | 12,000 | 4,096 |
| Research | 20 | 600,000 | 128,000 | 64,000 |

There is also a limit of 48 tool calls. New tool-result text sent to the model
is limited to 64,000 characters per result and 300,000 characters per request.
These character limits apply to the model view, not the full structured results
kept in turn records. Context and deadline checks reserve room for a final
answer and delivery, so a request can stop before these limits.
Missing provider usage counts against the budget using estimates and the full
output allowance. `Agent usage` logs distinguish reported and unknown usage.

## Capability contract

`RegisteredAgentTool` pairs a model-facing schema with a trusted handler, an
effect, an action class, and a `CapabilityContract`. The registry rejects
duplicate names and missing or inconsistent contracts.

The contract declares entity fields, filters, source channels, retained source
identities, and required access. `compile_capability_call` binds arguments to
that scope; `require_source_provenance` rejects successful channel reads that
omit or exceed it. Reports retain their source channels and access requirements
alongside their contents. A result without provenance must not become reusable
evidence.

The effect distinguishes reads, conversation state, artifacts, and commands.
The action class separately distinguishes reads, outputs, changes, and
irreversible changes. Change handlers prepare a `PreparedAction` with a preview
and precondition check. Confirmation and execution recheck permissions and
preconditions; planning a change does not execute it. Reads cannot depend on a
change result. Irreversible changes can share a preview only when they are all
the same kind. Command adapters live under `capabilities/`; the bridge uses the
registered command's interface and checks.

## Access and disclosure

`access.py` checks the member and bot can read the request channel, then checks
each evidence source and required role group. Private-thread reads also require
the requester to be a thread member. Retained history, reports, and knowledge
access are rechecked when reused and before delivery.

`disclosure.py` separately checks whether the destination audience may see all
sources. An incomplete member cache needs a provable permission structure;
uncertain readership does not authorize a public answer. When the audience
cannot be established, `PrivateAnswerView` offers requester-only controls.
The member-facing behavior, including explicit disclosure and the audit-log
exception, is described in [Privacy and AI](../users/privacy-and-ai.md#agent).
Remembered disclosure decisions apply only to that requester and conversation,
and only to subsets of the previously disclosed sources and access requirements.
They never bypass the requester's own access checks.

## Grounding

`engine/grounding.py` renders `[[message:ID]]` from messages read during the
request, using the recorded author, exact text, and source link. It escapes
source Markdown and mentions and rejects unknown or conflicting message
identities. A lexical guard rejects unsupported personal attributions and
specific false implications from Clash profile or roster fields.

`factual_answers.py` and `strict_answers.py` render supported narrow lookups from
typed evidence rather than generated numbers. A failed grounding check may
receive one answer-only repair round; an answer that still fails is withheld.
These checks cover defined patterns and lookups, not the truth of arbitrary
prose. Tests for new answer forms must exercise unsupported claims as well as
valid evidence.

## Conversation state

Conversations are scoped by guild, channel, and root message. Reply IDs locate
them, including after a restart. Turns retain the question, generated and
delivered answers, evidence, report IDs, source access, image references, and
delivery state. Nonces let delivery reconcile an uncertain send without asking
the model to generate the answer again.

Working conversations expire after six idle hours. Memory holds up to 128
conversations. Each conversation keeps at most 2,048 turns, 1 MiB of turn data
in total, and 512 reply references. Oldest turns are evicted to meet the turn
and byte limits. When a turn exceeds the byte limit, its full record is dropped;
its context excerpt is kept if it fits and marked as retention-limited.
Active requests are protected from idle pruning.
Persistence uses revision checks and settles a started write before cancellation
completes.
Context compilation selects whole authorized records and bounded checkpoints;
older retained turns remain available through the history tool.

Discord research jobs keep their own cursors, coverage, leases, and results.
Their six-hour idle lifetime is refreshed when touched. Action execution and
scheduled requests use separate durable state. See [Saved data](../reference/storage.md)
for file ownership and [Privacy and AI](../users/privacy-and-ai.md#agent)
for transcript and diagnostic retention; conversation expiry does not erase
those archives.

## Knowledge retrieval

`KnowledgeStore` builds an immutable catalogue and cached prompts for access
profiles. Approved, visible `always_loaded` sections form the core. The topic
index contains only public titles and filenames, even for privileged members;
restricted sections are still available through authorized retrieval.

`search_approved_knowledge` can search words, open up to five exact titles, or
continue a section by its ID. Titles are case-sensitive and take precedence
over a query; a section ID takes precedence over both. Search normalizes regular
English plurals, removes stop words, and ranks title and body matches with
length normalization. It is lexical retrieval, not exhaustive evidence that a
topic is absent.

Results include store status, coverage, pagination, and section continuation.
Hidden titles are indistinguishable from unknown titles. Consumers must preserve
partial results and follow continuation fields when they need complete text.
See [Maintaining agent knowledge](../operators/agent-knowledge.md) for file
format, reload behavior, and prompt limits.

## Testing

Agent tests live under `tests/features/agent`, grouped like the implementation.
Run them from the repository root:

```console
python -m unittest discover -s tests/features/agent -t .
python -m unittest discover -s tests
```

For a capability change, cover schema and dependency failures, lost access,
source provenance, partial results, and confirmation rechecks where relevant.
The `engine`, `knowledge`, `conversation`, and `actions` suites cover budgets,
grounding, retrieval, persistence, and private delivery. Provider-session tests
live in `tests/core/test_ai_infrastructure.py`. Follow
[Testing and verification](testing.md) for Discord and external-service checks.
