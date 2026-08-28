from __future__ import annotations


FIRST_PAGE_LABEL = "⏮️ First"
PREV_PAGE_LABEL = "◀️ Prev"
NEXT_PAGE_LABEL = "Next ▶️"
LAST_PAGE_LABEL = "Last ⏭️"

ADAPTIVE_JUMP_THRESHOLD = 3  # show First/Last when total_pages > this


def format_page_footer(page: int, total_pages: int, *, section: str | None = None) -> str:
    base = f"Page {page}/{total_pages}"
    return f"{section} • {base}" if section else base
