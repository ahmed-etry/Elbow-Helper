"""Availability feature package for examination."""

from .logic import DAY_ORDER
from .logic import ExaminationAvailabilityMixin
from .logic import _canonicalize_availability_text
from .logic import _clean_answer
from .logic import _format_availability_display
from .logic import _format_structured_availability_display
from .logic import _has_explicit_date
from .logic import _hours_to_time
from .logic import _normalize_availability_text
from .logic import _normalize_question
from .logic import _normalize_structured_windows
from .logic import _normalize_text
from .logic import _parse_date_from_text
from .logic import _parse_single_time_input
from .logic import _parse_time_token
from .logic import _resolve_timezone_offset
from .logic import _strip_invisible
from .logic import _strip_markdown
from .logic import parse_availability_windows
from .logic import parse_timezone_offset
from .overlaps import _all_overlap_windows_structured
from .overlaps import _format_structured_availability_examples
from .overlaps import _format_ticket_availability_display
from .overlaps import _next_overlap_window
from .overlaps import availability_matches
from .overlaps import availability_matches_structured
from .view import AvailabilityPromptView

__all__ = [
    "DAY_ORDER",
    "ExaminationAvailabilityMixin",
    "AvailabilityPromptView",
    "_all_overlap_windows_structured",
    "_canonicalize_availability_text",
    "_clean_answer",
    "_format_availability_display",
    "_format_structured_availability_display",
    "_format_structured_availability_examples",
    "_format_ticket_availability_display",
    "_has_explicit_date",
    "_hours_to_time",
    "_next_overlap_window",
    "_normalize_availability_text",
    "_normalize_question",
    "_normalize_structured_windows",
    "_normalize_text",
    "_parse_date_from_text",
    "_parse_single_time_input",
    "_parse_time_token",
    "_resolve_timezone_offset",
    "_strip_invisible",
    "_strip_markdown",
    "availability_matches",
    "availability_matches_structured",
    "parse_availability_windows",
    "parse_timezone_offset",
]
