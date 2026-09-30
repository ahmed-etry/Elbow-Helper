"""Execute confirmed changes outside button interactions."""

from __future__ import annotations

import asyncio
import logging
import json
from itertools import groupby
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

import discord

from ..access import require_access, require_disclosure_access
from ..commands.outcomes import CommandOutcome
from ..commands.private_view import PrivateCommandView
from ..message_parts import chunk_response
from ..wording import (
    ACTION_PREVIEW_UNIT_MANY, ACTION_PREVIEW_UNIT_ONE,
    ACTION_PROGRESS, ACTION_RUNNING,
    ACTION_STOP_BUTTON, ACTION_STOP_OWNER, COMMAND_CONFIRM_FAILED,
    ACTION_RUN_DONE, COMMAND_NO_CHANGES,
)
from .contracts import ActionClass, PreparedAction, check_bundle
from .repository import AgentActionRepository


LOGGER = logging.getLogger(__name__)
ACTION_TIMEOUT_SECONDS = 180


class ActionPreconditionChanged(ValueError):
    """A target changed after its preview was built."""


class StopActionRunView(discord.ui.View):
    def __init__(self, repository: AgentActionRepository, run_id: str, owner_id: int):
        super().__init__(timeout=None)
        self.repository = repository
        self.run_id = run_id
        self.owner_id = owner_id
        button = discord.ui.Button(label=ACTION_STOP_BUTTON, style=discord.ButtonStyle.secondary)
        button.callback = self.stop_run
        self.add_item(button)

    async def stop_run(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_STOP_OWNER, ephemeral=True)
            return
        await asyncio.to_thread(
            self.repository.request_stop, self.run_id,
            requester_id=self.owner_id,
        )
        await interaction.response.defer()

    def disable(self) -> None:
        for child in self.children:
            child.disabled = True


class AgentActionRunner:
    """Own live callbacks while SQLite owns run and audit state."""

    def __init__(self, *, bot: Any, repository: AgentActionRepository,
                 guild_id: int, enabled: bool = True,
                 undo_handlers: Mapping[str, Callable[[Any, Mapping[str, Any]], Awaitable[PreparedAction]]] | None = None,
                 on_finish: Callable[[Any, Mapping[str, Any], Any], Awaitable[None]] | None = None):
        self.bot = bot
        self.repository = repository
        self.guild_id = guild_id
        self.enabled = enabled
        self.undo_handlers = dict(undo_handlers or {})
        self.on_finish = on_finish
        self._tasks: set[asyncio.Task] = set()
        self._ready = asyncio.Event()

    def start(self) -> None:
        task = asyncio.create_task(self.recover())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()

    async def submit(self, context: Any, actions: tuple[PreparedAction, ...],
                     *, confirmer_id: int) -> str:
        if not self.enabled:
            raise RuntimeError("Agent actions are disabled")
        if confirmer_id != context.member.id:
            raise ValueError("Only the requester may confirm")
        await self._ready.wait()
        check_bundle(actions)
        message = context.source_message
        run_id = await asyncio.to_thread(
            self.repository.create_run,
            guild_id=context.guild.id, channel_id=message.channel.id,
            request_message_id=message.id, requester_id=context.member.id,
            confirmer_id=confirmer_id,
            steps=tuple({
                "name": action.path,
                "label": action.preview.summary or action.path,
                "class": action.action_class.value,
                "values": dict(action.values),
                "preview": action.preview.lines,
                "before": action.preview.before,
            } for action in actions),
        )
        task = asyncio.create_task(self._execute(run_id, context, actions))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def prepare_undo(self, context: Any, log_id: str) -> PreparedAction:
        if not self.enabled:
            raise RuntimeError("Agent actions are disabled")
        entry = await asyncio.to_thread(
            self.repository.log_entry, log_id, requester_id=context.member.id,
        )
        if entry is None or entry["action_class"] != "change" or entry["outcome"] != "completed":
            raise ValueError("This change is unavailable for undo")
        handler = self.undo_handlers.get(entry["action_name"])
        if handler is None or entry["before_json"] is None:
            raise ValueError("This change has no undo handler")
        action = await handler(context, {
            **entry,
            "targets": json.loads(entry["targets_json"]),
            "before": json.loads(entry["before_json"]),
            "after": json.loads(entry["after_json"]) if entry["after_json"] else None,
        })
        if action.action_class is not ActionClass.CHANGE:
            raise ValueError("Undo must be a reversible change")
        return action

    async def _execute(self, run_id: str, context: Any,
                       actions: tuple[PreparedAction, ...]) -> None:
        owner = uuid4().hex
        if not await asyncio.to_thread(self.repository.claim, run_id, owner=owner):
            return
        channel = context.source_message.channel
        view = StopActionRunView(self.repository, run_id, context.member.id)
        progress = None
        private_parts: list[str] = []
        private_files = []
        action_results: dict[str, Mapping[str, Any]] = {}
        outcome_status = "completed"
        try:
            progress = await channel.send(
                ACTION_RUNNING.format(
                    count=len(actions),
                    unit=ACTION_PREVIEW_UNIT_ONE if len(actions) == 1 else ACTION_PREVIEW_UNIT_MANY,
                ), view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for index, action in enumerate(actions):
                run = await asyncio.to_thread(self.repository.run, run_id)
                if run["stop_requested"]:
                    outcome_status = "stopped"
                    break
                if not await asyncio.to_thread(
                    self.repository.start_step, run_id, index, owner=owner,
                ):
                    outcome_status = "stopped"
                    break
                current_action = action
                try:
                    require_access(context.guild, context.member.id, channel)
                    await require_disclosure_access(context)
                    if action.bind is not None:
                        current_action = await action.bind(action_results)
                        if (current_action.action_class is not action.action_class
                                or current_action.bind is not None):
                            raise TypeError("Action binding returned an invalid change")
                        recorded_values = await asyncio.to_thread(
                            self.repository.set_step_values, run_id, index,
                            owner=owner, values=current_action.values,
                        )
                        if not recorded_values:
                            raise RuntimeError("Action targets could not be recorded")
                    if not await current_action.preview.recheck():
                        raise ActionPreconditionChanged("Action precondition changed")
                    async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
                        result = await current_action.run()
                    if not isinstance(result, CommandOutcome):
                        raise TypeError("Action returned an invalid result")
                    if result.status != "complete":
                        raise ValueError("Action did not complete")
                    if current_action.verify is not None and await current_action.verify() is False:
                        raise ValueError("Action could not be verified")
                    if result.visibility == "private":
                        private_parts.extend((result.text,) if result.text else ())
                        private_parts.extend(result.private_parts)
                        private_files.extend(result.attachments)
                    elif result.text:
                        await self._send_parts(channel, result.text)
                    recorded = await asyncio.to_thread(
                        self.repository.finish_step, run_id, index,
                        owner=owner, status="completed",
                        outcome={"status": result.status, "visibility": result.visibility,
                                 "result": result.result},
                        after=result.after,
                    )
                    if not recorded:
                        raise RuntimeError("Action result could not be recorded")
                    if action.step_id and result.result is not None:
                        action_results[action.step_id] = dict(result.result)
                    if progress is not None and index + 1 < len(actions):
                        try:
                            await progress.edit(
                                content=ACTION_PROGRESS.format(done=index + 1, total=len(actions)),
                                view=view,
                            )
                        except discord.DiscordException:
                            LOGGER.warning("Agent action progress could not be posted: run=%s", run_id)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    status = "uncertain" if _uncertain(error) else "failed"
                    if status == "uncertain" and current_action.verify is not None:
                        try:
                            if await current_action.verify() is True:
                                status = "completed"
                        except Exception:
                            LOGGER.exception("Agent action verification failed: run=%s step=%s", run_id, index)
                    if status != "completed":
                        outcome_status = "failed"
                    await asyncio.to_thread(
                        self.repository.finish_step, run_id, index,
                        owner=owner, status=status,
                        outcome={"error_class": type(error).__name__},
                    )
                    LOGGER.exception("Agent action failed: run=%s step=%s", run_id, index)
                    if status != "completed":
                        break
            await asyncio.to_thread(
                self.repository.finish_run, run_id, owner=owner, status=outcome_status,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Agent action run failed: run=%s", run_id)
            await asyncio.to_thread(
                self.repository.finish_run, run_id, owner=owner, status="failed",
            )
        finally:
            view.disable()
            run = await asyncio.to_thread(self.repository.run, run_id)
            report = self._report(run)
            chunks = chunk_response(report) or [report]
            private_view = (PrivateCommandView(context.member.id,
                                               tuple(private_parts), tuple(private_files))
                            if private_parts or private_files else None)
            reported = None
            try:
                if progress is not None:
                    await progress.edit(content=chunks[0],
                                        view=private_view if len(chunks) == 1 else None)
                    reported = progress
                    for index, chunk in enumerate(chunks[1:], start=1):
                        reported = await channel.send(
                            chunk,
                            view=private_view if index == len(chunks) - 1 else None,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                else:
                    for index, chunk in enumerate(chunks):
                        reported = await channel.send(
                            chunk,
                            view=private_view if index == len(chunks) - 1 else None,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                if private_view is not None:
                    private_view.message = reported
            except discord.DiscordException:
                LOGGER.exception("Agent action result could not be posted: run=%s", run_id)
            if self.on_finish is not None:
                try:
                    await self.on_finish(context, run, reported)
                except Exception:
                    LOGGER.exception("Agent action outcome could not be retained: run=%s", run_id)

    @staticmethod
    def _report(run: dict[str, Any]) -> str:
        completed = [step for step in run["steps"] if step["status"] == "completed"]
        summaries = []
        for _, items in groupby(completed, key=lambda step: (
                step["action_label"], step["action_class"],
            )):
            group = list(items)
            summaries.append(f"{group[0]['action_label']} ({len(group)})")
        finished = ", ".join(summaries) or COMMAND_NO_CHANGES
        remaining = [line.strip() or "-" for step in run["steps"]
                     if step["status"] != "completed"
                     for item in json.loads(step["preview_json"])
                     for line in item.split("\n")]
        if not remaining:
            return ACTION_RUN_DONE.format(finished=finished)
        return COMMAND_CONFIRM_FAILED.format(
            finished=finished, remaining="\n".join(remaining),
        )

    @staticmethod
    async def _send_parts(channel: Any, value: str) -> None:
        remaining = value
        while remaining:
            part = remaining[:1900]
            if len(remaining) > 1900:
                split_at = part.rfind("\n")
                if split_at > 0:
                    part = part[:split_at]
            await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
            remaining = remaining[len(part):].lstrip("\n")

    async def recover(self) -> None:
        try:
            wait_ready = getattr(self.bot, "wait_until_ready", None)
            if callable(wait_ready):
                await wait_ready()
            await asyncio.to_thread(self.repository.interrupt_incomplete,
                                    guild_id=self.guild_id)
            if not self.enabled:
                return
            runs = await asyncio.to_thread(self.repository.unreported_interruptions,
                                           guild_id=self.guild_id)
            for run in runs:
                guild = self.bot.get_guild(run["guild_id"])
                channel = guild.get_channel_or_thread(run["channel_id"]) if guild else None
                if channel is None and guild is not None:
                    try:
                        channel = await self.bot.fetch_channel(run["channel_id"])
                    except (discord.DiscordException, AttributeError):
                        continue
                    if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
                        continue
                if channel is None:
                    continue
                try:
                    for chunk in chunk_response(self._report(run)):
                        await channel.send(
                            chunk, allowed_mentions=discord.AllowedMentions.none(),
                        )
                except discord.DiscordException:
                    LOGGER.exception("Interrupted action run could not be reported: run=%s", run["run_id"])
                else:
                    await asyncio.to_thread(self.repository.mark_reported, run["run_id"])
        finally:
            self._ready.set()


def _uncertain(error: Exception) -> bool:
    return isinstance(error, (OSError, TimeoutError)) or (
        isinstance(error, discord.HTTPException)
        and getattr(error, "status", None) is not None
        and error.status >= 500
    )
