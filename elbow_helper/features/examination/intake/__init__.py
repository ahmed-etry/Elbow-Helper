"""Clan-promotion intake package."""

from .logic import NO_EXAM_TARGETS
from .logic import PROMO_ROUTE_MAP
from .logic import PROMO_SOURCES
from .logic import requires_exam
from .logic import is_valid_route
from .logic import valid_targets_for_source
from .view import ExaminationPromoIntakeMixin
from .view import PromoIntakeView
from .view import PromoRouteChangeView

__all__ = [
    "ExaminationPromoIntakeMixin",
    "NO_EXAM_TARGETS",
    "PROMO_ROUTE_MAP",
    "PROMO_SOURCES",
    "PromoIntakeView",
    "PromoRouteChangeView",
    "is_valid_route",
    "requires_exam",
    "valid_targets_for_source",
]
