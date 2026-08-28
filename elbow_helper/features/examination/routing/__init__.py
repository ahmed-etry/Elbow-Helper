"""Routing feature package for examination."""

from .flow import ExaminationRoutingFlowMixin
from .matching import ExaminationRoutingMatchingMixin
from .rendering import ExaminationRoutingRenderingMixin
from .view import ExamRoutingView


class ExaminationRoutingMixin(
    ExaminationRoutingRenderingMixin,
    ExaminationRoutingMatchingMixin,
    ExaminationRoutingFlowMixin,
):
    """Compose routing helpers and orchestration for examination."""


__all__ = ["ExaminationRoutingMixin", "ExamRoutingView"]
