"""Conversation-owned report contracts and bounded retention."""

from collections.abc import Iterable, MutableMapping, Sequence
from typing import Any, Protocol


MAX_REPORTS = 6
MAX_REPORT_PAYLOAD_BYTES = 1024 * 1024


class ReportArtifact(Protocol):
    report_id: str
    retained_bytes: int

    def manifest(self) -> dict[str, Any]: ...


class ArtifactCapacityError(ValueError):
    """A complete artifact cannot fit; never retain only some of its rows."""


def retain_report(
    reports: MutableMapping[str, ReportArtifact],
    report: ReportArtifact,
    *,
    protected_report_ids: Iterable[str] = (),
) -> None:
    """Keep complete artifacts, evicting oldest entries within one conversation.

    Bytes measure serialized report data, not interpreter object overhead.
    Protected evidence is retained with the new report or the whole operation fails.
    Capacity rejection is atomic and leaves previously retained reports intact.
    """
    retain_reports(
        reports, (report,), protected_report_ids=protected_report_ids,
    )


def retain_reports(
    reports: MutableMapping[str, ReportArtifact],
    additions: Sequence[ReportArtifact],
    *,
    protected_report_ids: Iterable[str] = (),
) -> None:
    """Atomically retain a complete linked set within count and byte bounds."""
    if (
        not additions
        or len({report.report_id for report in additions}) != len(additions)
    ):
        raise ValueError("Report additions must have distinct identities")
    if any(report.retained_bytes > MAX_REPORT_PAYLOAD_BYTES for report in additions):
        raise ArtifactCapacityError("Report exceeds the conversation payload budget")
    addition_ids = {report.report_id for report in additions}
    protected = set(protected_report_ids)
    if any(
        not isinstance(report_id, str)
        or report_id not in reports and report_id not in addition_ids
        for report_id in protected
    ):
        raise ValueError("Protected report is not retained")
    candidate = dict(reports)
    for report in additions:
        candidate.pop(report.report_id, None)
        candidate[report.report_id] = report
    protected.update(addition_ids)
    total = sum(item.retained_bytes for item in candidate.values())
    while len(candidate) > MAX_REPORTS or total > MAX_REPORT_PAYLOAD_BYTES:
        expired_id = next(
            (report_id for report_id in candidate if report_id not in protected),
            None,
        )
        if expired_id is None:
            raise ArtifactCapacityError(
                "Report and protected evidence exceed the conversation budget"
            )
        total -= candidate.pop(expired_id).retained_bytes
    reports.clear()
    reports.update(candidate)
