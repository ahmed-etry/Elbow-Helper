# Agent context measurements

These measurements let maintainers compare context policy changes without
mistaking an estimator change for a reduction in provider usage. The routing
baseline is `0293ffaa`; all replay questions, identities and tool results are
synthetic. No model or Discord calls are made by the replay.

## Token calibration

The operator archive was opened with SQLite `mode=ro` and `query_only=ON`.
Only aggregate measurements are retained here. Its 109 records contain 317
rounds: 10,858,830 prompt tokens, including 8,955,776 cached tokens (82.5%).
That is historical cache usage, not a prediction of the new route's bill.

Debug records omit full input prompts. The calibration therefore reconstructs
the first result batch of each checked plan using `model_view`, then measures
`second.prompt_tokens - first.prompt_tokens - first.completion_tokens`.
Subtracting completion usage accounts for reasoning retained by the provider.
Unique step IDs must match the recorded tool results; inputs under 1,001
characters and nonpositive growth are excluded. Of 54 candidate transitions,
42 remain after excluding rates beyond three median absolute log deviations.
This reduces mismatches from early answers, repairs and older result formats.
It cannot establish exact first-round token counts.

Least squares fits 0.1905 tokens per ASCII prose character and 0.6099 per ASCII
syntax/digit character. Non-ASCII data is too sparse to fit independently;
it uses 0.5 tokens per UTF-8 byte. The rounded working estimator uses 0.2, 0.6
and 0.5 respectively, rounding the final sum up.

| Same 42 reconstructed batches | Tokens |
| --- | ---: |
| Measured provider growth | 231,986 |
| Previous estimate | 433,217 |
| Calibrated estimate | 222,510 |

Aggregate overestimation falls from 86.7% to a 4.1% underestimate. Median
absolute relative error is 10.3%. This is a fit to the available transitions,
not an independent tokenizer benchmark. Billing and subsequent rounds still
use reported provider usage. The UTF-8 byte bound on result truncation remains
separate from this approximate estimator.

The full-route soft input target rises from 64,000 to 96,000 calibrated tokens;
the social target rises from 10,000 to 16,000. The 18,000 lookup target is removed.
The synthetic full request needs about 12,000 estimated tokens, leaving about
84,000 for recent history and carried evidence at the new full target. These
targets allow more retained context; cumulative request spending and provider
window checks continue to apply.

To reproduce calibration, set `PYTHONPATH=tests`, then run:

```powershell
python -m features.agent.conversation.calibrate_tokens path/to/transcripts.sqlite3
```

The command emits coefficients, counts and totals only, never archived text.

## Synthetic routing replay

Run the existing cost harness with `PYTHONPATH=tests`:

```powershell
python -m features.agent.engine.cost_scenarios
```

The same harness also runs against the baseline checkout. First-request sizes
include the system text, request and serialized tool definitions. Total input
counts every round's growing transcript. Both routes produce the same answers.

| Scenario | First input characters, before → after | Total input characters, before → after | Rounds, before → after |
| --- | ---: | ---: | ---: |
| Count | 21,097 → 44,230 | 21,097 → 89,272 | 1 → 2 |
| Extraction | 21,103 → 44,236 | 21,103 → 44,236 | 1 → 1 |
| Correction | 21,105 → 44,238 | 21,105 → 44,238 | 1 → 1 |
| Multi-source | 44,248 → 44,248 | 135,033 → 135,033 | 3 → 3 |

The simple cases' system prefix grows from 18,482 to 40,534 characters, matching
the existing full route. Every non-social scenario starts at high planning
effort. The synthetic count has no configured Clash client, so it exercises
the model route; a configured client still serves a fully matched bare clan
count with one read and zero model calls. Strict extraction retains its
existing one-round formatter.

| Scenario | Previous estimate on baseline | Calibrated estimate on baseline | Calibrated estimate after |
| --- | ---: | ---: | ---: |
| Count | 12,143 | 5,598 | 23,868 |
| Extraction | 12,146 | 5,599 | 11,787 |
| Correction | 12,147 | 5,599 | 11,787 |
| Multi-source | 78,595 | 36,177 | 36,177 |

The middle column applies the new estimator only to the baseline harness's
serialized inputs. Comparing it with the last column isolates routing cost.
Simple requests now send more context, and model-routed counts need an answer
round. Stable prefixes permit cache reuse; this offline replay does not measure
cache misses, latency, reasoning output or billed cost.

## Section boundaries

Knowledge paths include every heading level, with empty parents folded into
their children. Chunks target about 1,500 calibrated tokens including the path,
split only between paragraphs outside fenced blocks. A single oversized
paragraph or fenced block remains whole and uses the existing body paging.
Each part repeats the path and carries its ordinal; identical parts have
distinct section IDs. The topic index lists each path once with its part count.
Full paths open case-insensitively; leaf titles open only one visible path.
Changed paths and chunks change content-bound section IDs, so previous
**Post here** decisions may need renewal for those sections.
