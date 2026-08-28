"""Process-level asynchronous error handling."""

from __future__ import annotations

import asyncio
import logging

from .logging import TransientExternalFailurePolicy


LOGGER = logging.getLogger("elbow.asyncio")


def _describe_context_source(context: dict[str, object]) -> str:
    source = context.get("task") or context.get("future") or context.get("handle")
    if source is None:
        return "unknown"
    get_name = getattr(source, "get_name", None)
    if callable(get_name):
        try:
            name = get_name()
        except Exception:
            name = None
        if name:
            return str(name)
    return repr(source)


def handle_asyncio_exception(
    _loop: asyncio.AbstractEventLoop,
    context: dict[str, object],
) -> None:
    """Log unhandled asyncio failures with compact transient-failure handling."""

    error = context.get("exception")
    message = str(context.get("message") or "Unhandled asyncio exception")
    source = _describe_context_source(context)

    if isinstance(error, BaseException):
        level = (
            logging.WARNING
            if TransientExternalFailurePolicy.is_transient_exception(error)
            else logging.ERROR
        )
        LOGGER.log(
            level,
            "%s source=%s",
            message,
            source,
            exc_info=(type(error), error, error.__traceback__),
        )
        return

    details = {
        key: value
        for key, value in context.items()
        if key not in {"message", "task", "future", "handle"}
    }
    LOGGER.error("%s source=%s details=%s", message, source, details)


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Install the application's process-level asyncio exception handler."""

    loop.set_exception_handler(handle_asyncio_exception)
