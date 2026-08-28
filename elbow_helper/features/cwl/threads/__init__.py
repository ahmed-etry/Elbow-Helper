"""CWL thread registration, sticky embeds, and CC status workflows."""

from .commands import CwlThreadCommandMixin
from .listeners import CwlThreadListenerMixin
from .snapshots import CwlThreadSnapshotMixin
from .state import CwlThreadStateMixin
from .tasks import CwlThreadTasksMixin


class CwlThreadMixin(
    CwlThreadCommandMixin,
    CwlThreadListenerMixin,
    CwlThreadTasksMixin,
    CwlThreadSnapshotMixin,
    CwlThreadStateMixin,
):
    """Composed CWL thread feature."""
