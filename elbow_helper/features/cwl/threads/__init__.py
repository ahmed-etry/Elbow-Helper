"""CWL thread registration, sticky embeds, and CC status workflows."""

from .board import CwlThreadBoardMixin
from .commands import CwlThreadCommandMixin
from .listeners import CwlThreadListenerMixin
from .state import CwlThreadStateMixin
from .tasks import CwlThreadTasksMixin


class CwlThreadMixin(
    CwlThreadCommandMixin,
    CwlThreadListenerMixin,
    CwlThreadBoardMixin,
    CwlThreadTasksMixin,
    CwlThreadStateMixin,
):
    """Composed CWL thread feature."""
