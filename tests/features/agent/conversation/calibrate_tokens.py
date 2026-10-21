"""Read-only, aggregate token calibration; never print archived message content.

Run with PYTHONPATH=tests:
python -m features.agent.conversation.calibrate_tokens path/to/transcripts.sqlite3

Debug records do not retain full request prompts. Reconstruct the first result
batch of a checked plan and compare its size with prompt growth less the prior
completion (including retained reasoning). Compaction uses the real model view.
Early deterministic answers, repairs and old view formats can mismatch this
reconstruction; reject outliers with three median absolute log deviations.
"""

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import statistics

from elbow_helper.features.agent.conversation.context import estimate_tokens
from elbow_helper.features.agent.plan.results import model_view
from elbow_helper.features.agent.prompts import RESULT_ANSWER_INSTRUCTION


def _counts(text):
    prose = syntax = non_ascii = 0
    for char in text:
        if not char.isascii():
            non_ascii += len(char.encode("utf-8"))
        elif char.isalpha() or char.isspace():
            prose += 1
        else:
            syntax += 1
    return prose, syntax, non_ascii


def _samples(records):
    for record in records:
        rounds, plans = record.get("rounds", ()), record.get("plans", ())
        if len(rounds) < 2 or not plans:
            continue
        plan = plans[0]
        if not plan.get("check", {}).get("ok") or not isinstance(plan.get("plan"), dict):
            continue
        steps = plan["plan"].get("steps", ())
        ids = {step["id"] for step in steps}
        calls = [call for call in record.get("tool_calls", ()) if call["step_id"] in ids]
        if (not steps or len(calls) != len(steps)
                or len({call["step_id"] for call in calls}) != len(calls)
                or any(not isinstance(call.get("result"), dict) for call in calls)):
            continue
        first, second = (entry.get("usage", {}) for entry in rounds[:2])
        counts = (first.get("prompt_tokens"), first.get("completion_tokens"),
                  second.get("prompt_tokens"))
        if any(type(value) is not int for value in counts):
            continue
        growth = counts[2] - counts[0] - counts[1]
        text = json.dumps({
            "results": {call["step_id"]: model_view(call["result"]) for call in calls},
            "instruction": RESULT_ANSWER_INSTRUCTION,
        }, ensure_ascii=False)
        if growth > 0 and len(text) > 1000:
            yield text, growth


def summarize(records):
    samples = tuple(_samples(records))
    if not samples:
        raise ValueError("No reconstructable round transitions")
    rates = [math.log(tokens / len(text)) for text, tokens in samples]
    center = statistics.median(rates)
    deviation = statistics.median(abs(rate - center) for rate in rates)
    selected = [sample for sample, rate in zip(samples, rates)
                if abs(rate - center) <= 3 * deviation]
    # Fit prose and syntax coefficients; Unicode is too sparse to fit reliably.
    xx = xy = yy = xt = yt = 0
    old_total = new_total = actual_total = 0
    errors = []
    for text, tokens in selected:
        prose, syntax, non_ascii = _counts(text)
        target = tokens - non_ascii / 2
        xx += prose * prose
        xy += prose * syntax
        yy += syntax * syntax
        xt += prose * target
        yt += syntax * target
        old_total += (prose + 2 * syntax + 2 * non_ascii + 1) // 2
        estimated = estimate_tokens(text)
        new_total += estimated
        actual_total += tokens
        errors.append(abs(estimated - tokens) / tokens)
    determinant = xx * yy - xy * xy
    coefficients = {
        "ascii_prose": (xt * yy - yt * xy) / determinant if determinant else None,
        "ascii_syntax_digits": (yt * xx - xt * xy) / determinant if determinant else None,
        "non_ascii_bytes": 0.5,
    }
    rounds = [entry.get("usage", {}) for record in records for entry in record.get("rounds", ())]
    return {
        "records": len(records), "rounds": len(rounds),
        "prompt_tokens": sum(entry.get("prompt_tokens") or 0 for entry in rounds),
        "cache_hit_tokens": sum(entry.get("prompt_cache_hit_tokens") or 0 for entry in rounds),
        "candidate_transitions": len(samples), "selected_transitions": len(selected),
        "fitted_tokens_per_character": coefficients,
        "measured_growth_tokens": actual_total,
        "old_estimated_tokens": old_total, "new_estimated_tokens": new_total,
        "median_absolute_relative_error": statistics.median(errors),
    }


def calibrate(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        records = [json.loads(row[0]) for row in connection.execute(
            "SELECT record_json FROM operator_debug_records ORDER BY record_id",
        )]
    return summarize(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    print(json.dumps(calibrate(parser.parse_args().archive), indent=2))
