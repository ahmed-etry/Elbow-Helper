"""Audit archived efficiency runs without a provider or writes to their directory.

PYTHONPATH=tests python -m agent.engine.efficiency_replay RUN_DIRECTORY

The recorded usage is measured provider usage. The unchanged-schedule replay
estimates only result-dedup savings; it does not predict new model decisions.
This is not an accuracy evaluation. No questions, answers, identities or source
text are printed. Inputs are never changed.
"""

from collections import Counter
import json
from pathlib import Path
import sys

from elbow_helper.agent.conversation.context import estimate_tokens
from elbow_helper.agent.engine.evidence_delta import EvidenceDelta, signature


def inspect_record(record):
    usage = record["usage"]
    tools = record.get("tool_calls", [])
    calls = Counter(call["capability"] for call in tools)
    repeated = Counter(signature({"capability": call["capability"], "arguments": call["arguments"]})
                       for call in tools)
    errors = sum(bool((call.get("result") or {}).get("error")) for call in tools)
    original_characters = revised_characters = 0
    input_saved = 0
    for session in record.get("model_sessions", []):
        delta = EvidenceDelta()
        sources = {}
        saved_context = 0
        if not session.get("tools"):
            continue
        for row in session["rounds"]:
            for result in row.get("tool_results", []):
                try:
                    payload = json.loads(result["content"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), dict):
                    continue
                emitted, _ = delta.prepare(payload["results"], sources)
                previous = json.dumps(payload, ensure_ascii=False)
                revised = json.dumps({**payload, "results": emitted}, ensure_ascii=False)
                original_characters += len(previous)
                revised_characters += len(revised)
                saved_context += estimate_tokens(previous) - estimate_tokens(revised)
            input_saved += saved_context
            for call in row.get("tool_calls", []):
                if call.get("name") != "submit_request_plan":
                    continue
                try:
                    plan = json.loads(call["arguments"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(plan, dict):
                    continue
                sources.update({step["id"]: {key: step.get(key) for key in ("capability", "arguments")}
                                for step in plan.get("steps", []) if isinstance(step, dict) and "id" in step})
    observed_input = usage["prompt_tokens"]
    return {
        "observed": {"rounds": record["model_rounds"], "prompt_tokens": observed_input,
                     "completion_tokens": usage["completion_tokens"], "tool_calls": len(tools),
                     "failed_tool_calls": errors},
        "tool_counts": dict(sorted(calls.items())),
        "identical_repeats": sum(count - 1 for count in repeated.values()),
        "unchanged_schedule_replay": {
            "rounds": record["model_rounds"],
            "result_characters_before": original_characters,
            "result_characters_after": revised_characters,
            "prompt_tokens_estimated": observed_input - input_saved,
            "saved_prompt_tokens_estimated": input_saved,
        },
    }


def measure(directory, suite_name="retest-reasoning"):
    root = Path(directory)
    suites = []
    for path in root.glob("*-run.json"):
        suite = json.loads(path.read_text(encoding="utf-8-sig"))
        if suite_name in suite.get("suite", ""):
            suites.append((path.name, suite))
    if not suites:
        raise ValueError(f"No {suite_name} suite found")
    path, suite = max(suites)
    cases = {}
    for case in suite["results"]:
        # Archived manifests use Windows paths, including when replayed elsewhere.
        source = root / case["record"].replace("\\", "/").rsplit("/", 1)[-1]
        cases[case["name"]] = inspect_record(json.loads(source.read_text(encoding="utf-8-sig")))
    return {"suite_record": path, "method": "archived usage; unchanged schedule; heuristic dedup savings",
            "cases": cases}


if __name__ == "__main__":
    print(json.dumps(measure(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "retest-reasoning"), indent=2))
