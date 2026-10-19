"""Classify failed cleanup edits without hiding unexpected Discord failures."""

import discord


def log_cleanup_failure(logger, message, error):
    # Deleted messages/webhooks and expired interaction webhook tokens cannot
    # be edited. Local view state already prevents their controls being reused.
    expected = isinstance(error, discord.NotFound) or (
        isinstance(error, discord.HTTPException)
        and error.status == 401 and error.code == 50027
    )
    if expected:
        logger.debug("%s: %s", message, error)
    else:
        logger.warning("%s: %s", message, error, exc_info=True)
