"""Exact requester instructions, separate from generated summaries or approvals."""

from dataclasses import asdict, dataclass, replace
import json
from uuid import uuid4


MAX_ACTIVE_INSTRUCTIONS = 32
MAX_INSTRUCTION_REVISIONS = 128
MAX_INSTRUCTION_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class TaskInstruction:
    instruction_id: str
    label: str
    quote: str
    member_id: int
    source_message_id: int
    source_channel_id: int
    created_at: str
    supersedes_id: str | None = None
    retired_by_message_id: int | None = None

    @property
    def active(self) -> bool:
        return self.retired_by_message_id is None


@dataclass(frozen=True, slots=True)
class WorkingState:
    instructions: tuple[TaskInstruction, ...] = ()
    version: int = 0

    def remember(
        self, *, label: str, quote: str, request_text: str, member_id: int,
        message_id: int, channel_id: int, created_at: str, replaces_id: str | None = None,
    ) -> tuple["WorkingState", TaskInstruction]:
        if not quote.strip() or len(quote) > 2000 or quote not in request_text:
            raise ValueError("The instruction must be an exact quote from the current request.")
        if not label.strip() or len(label) > 80:
            raise ValueError("The instruction label must contain 1 to 80 characters.")
        rows = list(self.instructions)
        if replaces_id is not None:
            previous = self._owned_active(replaces_id, member_id)
            rows[rows.index(previous)] = replace(previous, retired_by_message_id=message_id)
        else:
            for row in rows:
                if row.active and row.member_id == member_id and row.quote == quote and row.label == label:
                    return self, row
        instruction = TaskInstruction(uuid4().hex, label, quote, member_id, message_id,
                                      channel_id, created_at, replaces_id)
        rows.append(instruction)
        if sum(row.active for row in rows) > MAX_ACTIVE_INSTRUCTIONS:
            raise ValueError("Too many active task instructions. Retire instructions that no longer apply.")
        return self._bounded(rows), instruction

    def retire(self, instruction_id: str, *, member_id: int, message_id: int) -> "WorkingState":
        previous = self._owned_active(instruction_id, member_id)
        return self._bounded([replace(row, retired_by_message_id=message_id) if row is previous else row
                              for row in self.instructions])

    def _owned_active(self, instruction_id: str, member_id: int) -> TaskInstruction:
        previous = next((row for row in self.instructions if row.instruction_id == instruction_id and row.active), None)
        if previous is None:
            raise ValueError("That active task instruction is not available.")
        if previous.member_id != member_id:
            raise ValueError("Only the member who supplied this instruction can replace or retire it.")
        return previous

    def _bounded(self, rows: list[TaskInstruction]) -> "WorkingState":
        while (len(rows) > MAX_INSTRUCTION_REVISIONS
               or len(json.dumps([asdict(row) for row in rows], ensure_ascii=False).encode("utf-8")) > MAX_INSTRUCTION_BYTES):
            expired = next((index for index, row in enumerate(rows) if not row.active), None)
            if expired is None:
                raise ValueError("Active task instructions exceed the retention budget.")
            rows.pop(expired)
        return WorkingState(tuple(rows), self.version + 1)
