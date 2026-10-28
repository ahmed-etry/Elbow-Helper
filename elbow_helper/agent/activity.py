"""Own the typing refresh task through its cancellation before sending a reply."""

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def reply_typing(channel):
    activity = channel.typing()
    try:
        async with activity:
            yield
    finally:
        # discord.py cancels this task without joining it. A pending typing
        # request must finish cancelling before the final message is sent.
        task = getattr(activity, "task", None)
        if isinstance(task, asyncio.Task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
