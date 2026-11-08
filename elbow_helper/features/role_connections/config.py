"""Local constants for the role connections package."""

from __future__ import annotations

from elbow_helper.infrastructure.persistence.documents import Document

STATE_FILE = Document("role_connections")
ROLE_CHANGE_DELAY_SECONDS = 10
CONNECTIONS_PER_PAGE = 4
SCAN_PREVIEW_PER_PAGE = 3
SELECTOR_PAGE_SIZE = 25
INVALID_DEPENDENCY_MESSAGE = "A role connection cannot depend on itself."
PERSISTENCE_ERROR_MESSAGE = "That role connection couldn't be saved."

