"""Application logging configuration and transient-failure policy."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import re
import time

import discord

from .paths import ApplicationPaths


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(log_tag)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "bot.log"
DEBUG_LOG_FILE_NAME = "bot.debug.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
CONSOLE_LOG_LEVEL = logging.WARNING
FILE_LOG_LEVEL = logging.INFO
CONSOLE_INFO_ALLOWLIST = {
    "elbow.boot",
    "elbow_helper.features.member_lifecycle",
}
TRANSIENT_LOG_DETAIL_MAX_CHARS = 260
TRANSIENT_DUPLICATE_COOLDOWN_SECONDS = 300.0


class TransientExternalFailurePolicy:
    """Classify retryable external failures so handlers can log them compactly."""

    _transient_text_markers = (
        "temporary failure in name resolution",
        "name or service not known",
        "getaddrinfo failed",
        "nodename nor servname",
        "cannot connect to host",
        "connect call failed",
        "connection reset",
        "connection aborted",
        "connection refused",
        "network is unreachable",
        "host is unreachable",
        "server disconnected",
        "timed out",
        "timeout",
    )
    _message_only_markers = (
        "temporary failure in name resolution",
        "name or service not known",
        "getaddrinfo failed",
        "nodename nor servname",
        "cannot connect to host",
        "connect call failed",
        "connection reset",
        "connection aborted",
        "connection refused",
        "network is unreachable",
        "host is unreachable",
        "server disconnected",
        "timed out",
    )
    _transient_exception_names = {
        "apiconnectionerror",
        "clientconnectionerror",
        "clientconnectorcertificateerror",
        "clientconnectordnserror",
        "clientconnectorerror",
        "clientconnectorsslerror",
        "clientoserror",
        "connecterror",
        "connecttimeout",
        "connectionerror",
        "gatewaynotfound",
        "networkerror",
        "readerror",
        "readtimeout",
        "remoteprotocolerror",
        "serverconnectionerror",
        "serverdisconnectederror",
        "timeout",
        "timeouterror",
    }
    _transient_statuses = {408, 409, 425, 429, 500, 502, 503, 504}

    @classmethod
    def _iter_exception_chain(cls, exc: BaseException):
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            yield current
            current = current.__cause__ or current.__context__

    @classmethod
    def _has_transient_status(cls, exc: BaseException) -> bool:
        status = getattr(exc, "status", None)
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            return False
        return status_int in cls._transient_statuses or 500 <= status_int < 600

    @classmethod
    def _matches_transient_exception(cls, exc: BaseException) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True
        if isinstance(exc, discord.HTTPException):
            return cls._has_transient_status(exc)

        name = exc.__class__.__name__.lower()
        if name in cls._transient_exception_names:
            return True
        if cls._has_transient_status(exc):
            return True

        text = str(exc).lower()
        return any(marker in text for marker in cls._transient_text_markers)

    @classmethod
    def is_transient_exception(cls, exc: BaseException | None) -> bool:
        if exc is None:
            return False
        return any(cls._matches_transient_exception(item) for item in cls._iter_exception_chain(exc))

    @classmethod
    def is_transient_record(cls, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1] is not None:
            return cls.is_transient_exception(record.exc_info[1])
        if record.levelno < logging.WARNING:
            return False
        message = record.getMessage().lower()
        return any(marker in message for marker in cls._message_only_markers)

    @classmethod
    def exception_summary(cls, exc: BaseException | None) -> str:
        if exc is None:
            return "transient external failure"
        parts: list[str] = []
        for item in cls._iter_exception_chain(exc):
            status = getattr(item, "status", None)
            name = item.__class__.__name__
            detail = re.sub(r"\s+", " ", str(item)).strip()
            if status is not None:
                name = f"{name} status={status}"
            if detail:
                parts.append(f"{name}: {detail}")
            else:
                parts.append(name)
        summary = " caused by ".join(parts) if parts else exc.__class__.__name__
        if len(summary) > TRANSIENT_LOG_DETAIL_MAX_CHARS:
            return f"{summary[:TRANSIENT_LOG_DETAIL_MAX_CHARS - 3]}..."
        return summary

    @classmethod
    def record_fingerprint(cls, record: logging.LogRecord) -> tuple[object, ...]:
        msg_template = record.msg if isinstance(record.msg, str) else record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            status = None
            name = exc.__class__.__name__
            for item in cls._iter_exception_chain(exc):
                if cls._matches_transient_exception(item):
                    name = item.__class__.__name__
                    status = getattr(item, "status", None)
                    break
            return (record.name, record.pathname, record.lineno, msg_template, name, status)
        return (record.name, record.pathname, record.lineno, msg_template)


class UnifiedLogFormatter(logging.Formatter):
    """Standardize logger tags and message prefixes across the bot."""

    @staticmethod
    def _build_tag(logger_name: str) -> str:
        if logger_name.startswith("elbow_helper.features."):
            return f"[{logger_name.split('.')[-1].upper()}]"
        if logger_name == "elbow.boot":
            return "[BOOT]"
        if logger_name.startswith("discord."):
            return f"[{logger_name.upper()}]"
        return f"[{logger_name.upper()}]"

    def format(self, record: logging.LogRecord) -> str:
        record.log_tag = self._build_tag(record.name)
        return super().format(record)


class SuppressDiscordResumeFilter(logging.Filter):
    """Hide Discord gateway session-resume info spam while keeping other events."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "discord.gateway":
            if "has successfully RESUMED session" in record.getMessage():
                return False
        return True


class SuppressNoisyExternalConsoleFilter(logging.Filter):
    """Drop low-value third-party retry chatter that is already handled upstream."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        if record.name == "discord.http" and (
            "we are being rate limited" in message
            or ("responded with 429" in message and "retrying in" in message)
        ):
            return False
        return True


class CompactTransientDuplicateFilter(logging.Filter):
    """Throttle repeated transient failures per handler."""

    def __init__(self, cooldown_seconds: float):
        super().__init__()
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._state: dict[tuple[object, ...], dict[str, float | int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if not TransientExternalFailurePolicy.is_transient_record(record):
            return True

        now = time.monotonic()
        key = TransientExternalFailurePolicy.record_fingerprint(record)
        state = self._state.get(key)
        if state is not None:
            last_emit = float(state.get("last_emit") or 0.0)
            if self.cooldown_seconds and (now - last_emit) < self.cooldown_seconds:
                state["suppressed"] = int(state.get("suppressed") or 0) + 1
                return False

        suppressed = int((state or {}).get("suppressed") or 0)
        record.transient_suppressed_count = suppressed
        self._state[key] = {"last_emit": now, "suppressed": 0}
        return True


class CompactTransientFormatter(UnifiedLogFormatter):
    """Format retryable external failures without traceback walls."""

    def _format_without_exception(self, record: logging.LogRecord) -> str:
        exc_info = record.exc_info
        exc_text = record.exc_text
        record.exc_info = None
        record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text

    def format(self, record: logging.LogRecord) -> str:
        if not TransientExternalFailurePolicy.is_transient_record(record):
            return super().format(record)

        if record.exc_info:
            base = self._format_without_exception(record)
            summary = TransientExternalFailurePolicy.exception_summary(record.exc_info[1])
            notes = [f"transient external failure: {summary}", "traceback suppressed"]
        else:
            base = super().format(record)
            notes = ["transient external failure"]

        suppressed = int(getattr(record, "transient_suppressed_count", 0) or 0)
        if suppressed:
            notes.append(f"suppressed {suppressed} similar logs")
        return f"{base} ({'; '.join(notes)})"


class ConsoleAllowlistFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= CONSOLE_LOG_LEVEL:
            return True
        if record.levelno == logging.INFO:
            if record.name in CONSOLE_INFO_ALLOWLIST:
                return True
            return any(record.name.startswith(f"{name}.") for name in CONSOLE_INFO_ALLOWLIST)
        return False


def log_box(logger: logging.Logger, lines: tuple[str, ...] | list[str]) -> None:
    """Write a compact bordered block to a logger."""

    if not lines:
        return
    width = max(len(line) for line in lines)
    border = "+" + "-" * (width + 2) + "+"
    for line in (border, *(f"| {line.ljust(width)} |" for line in lines), border):
        logger.info(line)


def configure_logging(paths: ApplicationPaths) -> None:
    """Configure console and rotating file handlers for the application."""

    paths.log_directory.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    formatter = UnifiedLogFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    compact_formatter = CompactTransientFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.addFilter(SuppressNoisyExternalConsoleFilter())
    console_handler.addFilter(
        CompactTransientDuplicateFilter(TRANSIENT_DUPLICATE_COOLDOWN_SECONDS)
    )
    console_handler.addFilter(ConsoleAllowlistFilter())
    console_handler.setFormatter(compact_formatter)
    root_logger.addHandler(console_handler)

    info_file_handler = RotatingFileHandler(
        paths.log_directory / LOG_FILE_NAME,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    info_file_handler.setLevel(FILE_LOG_LEVEL)
    info_file_handler.addFilter(
        CompactTransientDuplicateFilter(TRANSIENT_DUPLICATE_COOLDOWN_SECONDS)
    )
    info_file_handler.setFormatter(compact_formatter)
    root_logger.addHandler(info_file_handler)

    debug_file_handler = RotatingFileHandler(
        paths.log_directory / DEBUG_LOG_FILE_NAME,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    debug_file_handler.setLevel(logging.DEBUG)
    debug_file_handler.setFormatter(formatter)
    root_logger.addHandler(debug_file_handler)

    logging.getLogger("discord.gateway").addFilter(SuppressDiscordResumeFilter())
    for logger_name in ("aiohttp.access", "httpx", "openai", "matplotlib", "PIL", "urllib3"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
