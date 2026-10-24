"""Keep generated confirmation invitations tied to prepared changes."""

import re


_CONFIRMATION = re.compile(
    r"\bconfirm[\s,]+(?:and\b|to\s+(?:proceed|continue|apply|save|add|remove|delete|schedule)\b)"
    r"|\b(?:click|press|tap|hit)\s+(?:the\s+)?[\"'`*]*confirm\b"
    r"|\b(?:once|when|after|if)\s+(?:you\s+)?(?:confirm|approve)\b"
    r"|\b(?:waiting|awaiting|ready|pending|requires?|needs?)\b[^.!?\n]{0,60}"
    r"\byour\s+(?:confirmation|approval)\b"
    r"|\b(?:please\s+confirm|confirm\s+(?:this|that|it))\s*[.!?]*$"
    r"|^\s*confirm\s*[.!?]*$"
    r"|\bi(?:['’]ll|\s+will)\s+(?:add|remove|delete|schedule|save|send|change|set)\b",
    re.IGNORECASE | re.MULTILINE,
)

UNPREPARED_CHANGE_INSTRUCTION = (
    "No change has been prepared and there is no confirmation preview. "
    "If tools are available, submit a plan using the change capability to prepare the "
    "requested action. Otherwise explain what is missing or unavailable using the "
    "request and tool results. Preserve supported facts. Do not ask the member to "
    "confirm, promise a later change, or say it was done. Text cannot create a preview."
)


def unprepared_change_issue(answer, proposals):
    # Quoted messages and code samples are evidence or writing help, not an
    # invitation from the agent to execute a change.
    prose = re.sub(r"```.*?```", "", answer, flags=re.DOTALL)
    prose = "\n".join(line for line in prose.splitlines() if not line.lstrip().startswith(">"))
    prose = prose.replace("*", "").replace("`", "")
    if not proposals and _CONFIRMATION.search(prose):
        return UNPREPARED_CHANGE_INSTRUCTION
    return ""
