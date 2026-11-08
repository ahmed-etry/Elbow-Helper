"""Feature result facade over synthetic scorer and selection methods."""

from types import SimpleNamespace

from elbow_helper.features.cwl.queries import CwlQueries


class ResultQueries(SimpleNamespace):
    results = CwlQueries.results
    result_snapshot = CwlQueries.result_snapshot
