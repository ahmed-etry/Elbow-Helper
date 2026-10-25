"""Send repeated evidence by reference while retaining full typed observations."""

import json

from ..plan.results import compact_result


def signature(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def access_refused(result):
    """Only explicit authorization failures are terminal, never empty/failed reads."""
    return bool(result.get("error") and (
        result.get("required_access") or result.get("cause") == "access_denied"
        or "access_denied" in result.get("flags", {}).get("limits", ())
    ))


class EvidenceDelta:
    def __init__(self):
        self.results = {}
        self.sections = {}
        self.knowledge_statuses = set()
        self.messages = {}
        self.opened_titles = set()

    def _messages(self, step_id, value):
        """Overlap between search, context and history is the same observation.

        Preserve changed text and new annotations. Full rows remain in the
        runner and grounding store; only the provider view uses references.
        """
        result = dict(value)
        for field in ("messages", "matches"):
            rows = value.get(field)
            if isinstance(rows, dict) and set(rows) == {"columns", "rows"}:
                rows = [dict(zip(rows["columns"], row)) for row in rows["rows"]]
            if not isinstance(rows, list):
                continue
            fresh, reused, references = [], [], []
            for row in rows:
                if not isinstance(row, dict) or not row.get("source"):
                    fresh.append(row)
                    continue
                # Context and search attach different flags to the same text.
                core = {key: row.get(key) for key in (
                    "source", "message_id", "author_id", "author", "timestamp", "content",
                )}
                key = signature(core)
                if key in self.messages:
                    reused.append({"reference": self.messages[key], **{
                        name: item for name, item in row.items()
                        if name not in core and name not in ("channel", "channel_id")
                    }})
                else:
                    fresh.append(row)
                    reference = {"step": step_id, "path": [field, len(fresh) - 1]}
                    self.messages[key] = reference
                    references.append(reference)
            result[field] = compact_result(fresh)
            if isinstance(result[field], dict):
                for reference in references:
                    reference["path"].insert(1, "rows")
            if reused:
                result["reused_" + field] = reused
        return result

    def prepare(self, results, sources=None):
        emitted, changed = {}, False
        for step_id, value in results.items():
            key = signature({"result": value, "source": (sources or {}).get(step_id)})
            if key in self.results:
                emitted[step_id] = {"reused_result": {"step": self.results[key], "path": []},
                                    "flags": value.get("flags", {})}
                continue
            self.results[key] = step_id
            if not isinstance(value.get("sections"), list):
                emitted[step_id] = self._messages(step_id, value)
                changed = True
                continue
            fresh, reused = [], []
            for section in value["sections"]:
                section_key = signature(section)
                if section_key in self.sections:
                    reused.append(self.sections[section_key])
                else:
                    fresh.append(section)
                    # Reference the emitted array, which can omit earlier duplicates.
                    self.sections[section_key] = {"step": step_id, "path": ["sections", len(fresh) - 1]}
                if not section.get("truncated") and section.get("next_section_offset") is None:
                    self.opened_titles.add(section.get("title", ""))
            status = signature({field: value.get(field) for field in (
                "scope", "store_status", "available_privately", "match_status", "error",
            )})
            changed |= bool(fresh) or status not in self.knowledge_statuses
            # Repeated leading matches can still expose a different search's
            # continuation. Keep genuine missing pages reachable.
            changed |= value.get("next_offset") is not None or any(
                section.get("next_section_offset") is not None for section in value["sections"]
            )
            self.knowledge_statuses.add(status)
            emitted[step_id] = {**value, "sections": fresh}
            if reused:
                emitted[step_id]["reused_sections"] = reused
                emitted[step_id]["already_opened_titles"] = sorted(self.opened_titles - {""})
                emitted[step_id]["reuse_instruction"] = (
                    "Use the already opened sections. Lexical pagination ranks word matches; "
                    "it does not establish another requirement or a missing numeric cutoff. "
                    "Only open a distinct missing heading or a relevant continuation."
                )
        return emitted, changed
