"""Family-record reads, deliberately not wired into any existing consumer.

Times are UTC epoch microseconds. Pagination is stable for a frozen record;
callers paging a changing record should reuse an explicit ``as_of`` value.
"""

from .membership import MembershipReads
from .people import PeopleReads

__all__ = ["MembershipReads", "PeopleReads"]
