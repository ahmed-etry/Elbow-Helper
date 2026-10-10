"""Execute confirmed changes outside button interactions."""

from __future__ import annotations

import asyncio
import logging
import json
from dataclasses import dataclass, field, replace
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

import discord
from elbow_helper.discord.application_emojis import loading_status

from ..access import require_access, require_access_requirements, accessible_message_channel, AgentAccessLost, require_evidence_access
from ..disclosure import can_disclose_provenance
from .outcomes import ActionOutcome
from .private_view import PrivateResultView
from ..text import chunk_response
from ..wording import (
    action_progress_status,
    ACTION_STOP_BUTTON, ACTION_STOP_OWNER,
)
from .contracts import ActionClass, PreparedAction, check_bundle
from .store import AgentActionRepository
from .report import format_run_report


LOGGER = logging.getLogger(__name__)
ACTION_TIMEOUT_SECONDS = 180
STOP_BUTTON_DELAY_SECONDS = 5


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
        self.button = button
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


@dataclass(slots=True)
class ActionRunOutput:
    private_parts: list[str] = field(default_factory=list)
    private_files: list[Any] = field(default_factory=list)
    results: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    step_outcomes: dict[int, tuple[str, Mapping[str, Any]]] = field(default_factory=dict)
    posted_here: int = 0
    private_result: PrivateResultView | None = None
    public_details: bool = False


class AgentActionRunner:
    """Own live callbacks while SQLite owns run and audit state."""

    def __init__(self, *, bot: Any, repository: AgentActionRepository,
                 guild_id: int,
                 undo_handlers: Mapping[str, Callable[[Any, Mapping[str, Any]], Awaitable[PreparedAction]]] | None = None,
                 on_finish: Callable[[Any, Mapping[str, Any], Any], Awaitable[None]] | None = None):
        self.bot = bot
        self.repository = repository
        self.guild_id = guild_id
        self.undo_handlers = dict(undo_handlers or {})
        self.on_finish = on_finish
        self._tasks: set[asyncio.Task] = set()
        self._runs: dict[str, asyncio.Task] = {}
        self._ready = asyncio.Event()

    def start(self) -> None:
        task = asyncio.create_task(self.recover())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()

    async def submit(self, context: Any, actions: tuple[PreparedAction, ...],
                     *, confirmer_id: int, progress_message: Any = None,
                     private_result: PrivateResultView | None = None) -> str:
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
        task = asyncio.create_task(self._execute(
            run_id, context, actions, progress_message, private_result,
        ))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._runs[run_id] = task
        task.add_done_callback(lambda _: self._runs.pop(run_id, None))
        return run_id

    async def wait_run(self, run_id: str) -> Mapping[str, Any] | None:
        task = self._runs.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        return await asyncio.to_thread(self.repository.run, run_id)

    async def prepare_undo(self, context: Any, log_id: str) -> PreparedAction:
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
                       actions: tuple[PreparedAction, ...],
                       progress_message: Any = None,
                       private_result: PrivateResultView | None = None) -> None:
        owner = uuid4().hex
        if not await asyncio.to_thread(self.repository.claim, run_id, owner=owner):
            return
        LOGGER.info("Agent action run started: run=%s requester=%s steps=%s",
                    run_id, context.member.id, len(actions))
        channel = getattr(context, "delivery_channel", None) or context.source_message.channel
        view = StopActionRunView(self.repository, run_id, context.member.id)
        view.clear_items()
        progress = None
        stop_task = None
        output = ActionRunOutput(private_result=private_result)
        outcome_status = "completed"
        try:
            progress = await self._show_progress(
                channel, progress_message, view,
                await self._progress_text(actions[0], 0, len(actions)),
            )
            stop_task = asyncio.create_task(self._show_stop_later(progress, view, run_id))
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
                if index:
                    try:
                        await progress.edit(
                            content=await self._progress_text(action, index, len(actions)),
                            view=view,
                        )
                    except discord.DiscordException:
                        LOGGER.warning("Agent action progress could not be posted: run=%s", run_id)
                step_status = await self._run_step(
                    run_id, index, owner, context, action, channel, progress, view, output,
                    total=len(actions),
                )
                LOGGER.info("Agent action step finished: run=%s step=%s action=%s status=%s",
                            run_id, index, action.path, step_status)
                if step_status != "completed":
                    outcome_status = "failed"
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
            if stop_task is not None:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
            view.disable()
            await self._post_report(run_id, context, channel, progress, output)
            view.stop()

    @staticmethod
    async def _show_progress(channel: Any, progress_message: Any, view: discord.ui.View,
                             status: str):
        if progress_message is not None:
            try:
                await progress_message.edit(content=status, view=view)
                return progress_message
            except discord.DiscordException:
                LOGGER.warning("Agent preview could not show run progress")
        return await channel.send(status, view=view,
                                  allowed_mentions=discord.AllowedMentions.none())

    async def _progress_text(self, action, index, total):
        label = action.preview.summary
        status = action_progress_status(label) if label else action.path
        if total > 1:
            status += f" ({index + 1} of {total})"
        return await loading_status(self.bot, status)

    @staticmethod
    async def _show_stop_later(progress, view, run_id):
        await asyncio.sleep(STOP_BUTTON_DELAY_SECONDS)
        view.add_item(view.button)
        try:
            await progress.edit(view=view)
        except discord.DiscordException:
            LOGGER.warning("Agent action Stop button could not be posted: run=%s", run_id)

    async def _run_step(
        self, run_id, index, owner, context, action, channel, progress, view,
        output: ActionRunOutput, *, total: int,
    ) -> str:
        current_action = action
        try:
            require_access(context.guild, context.member.id, context.source_message.channel)
            await require_evidence_access(context)
            details_hidden = await self._detail_visibility(context, action)
            if action.bind is not None:
                current_action = await action.bind(output.results)
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
            if not isinstance(result, ActionOutcome):
                raise TypeError("Action returned an invalid result")
            if result.status != "complete":
                raise ValueError("Action did not complete")
            if current_action.verify is not None and await current_action.verify() is False:
                raise OSError("Action could not be verified")
            if details_hidden:
                result = replace(result, visibility="private")
            if result.visibility == "private":
                output.private_parts.extend((result.text,) if result.text else ())
                output.private_parts.extend(result.private_parts)
                output.private_files.extend(result.attachments)
            elif result.text:
                output.public_details = True
            if not await self._record_completed(run_id, index, owner, result, output):
                return "uncertain"
            if result.posted_in == channel.id:
                output.posted_here += 1
            if action.step_id and result.result is not None:
                output.results[action.step_id] = dict(result.result)
            return "completed"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return await self._failed_step(
                run_id, index, owner, current_action, error, output,
            )

    async def _record_completed(self, run_id, index, owner, result, output) -> bool:
        try:
            recorded = await asyncio.to_thread(
                self.repository.finish_step, run_id, index, owner=owner, status="completed",
                outcome={"status": result.status, "visibility": result.visibility,
                         "result": result.result,
                         "text": result.text if result.visibility == "public" else ""},
                after=result.after,
            )
        except Exception:
            LOGGER.exception("Agent action result could not be recorded: run=%s step=%s",
                             run_id, index)
            recorded = False
        if not recorded:
            output.step_outcomes[index] = ("uncertain", {})
            LOGGER.error("Agent action completion is unconfirmed: run=%s step=%s", run_id, index)
        return recorded

    async def _detail_visibility(self, context, action) -> bool:
        if action.preview.detail_access:
            require_access_requirements(
                context.guild, context.member.id, action.preview.detail_access,
            )
        for identifier in action.preview.detail_sources:
            if await accessible_message_channel(context, identifier) is None:
                raise AgentAccessLost("Action detail source is no longer readable")
        details_hidden = action.details_hidden
        if action.preview.detail_sources or action.preview.detail_access:
            details_hidden |= not await can_disclose_provenance(
                context, action.preview.detail_sources, action.preview.detail_access,
            )
        return details_hidden

    async def _failed_step(self, run_id, index, owner, current_action, error, output) -> str:
        status = ("permission" if isinstance(error, discord.Forbidden)
                  else "uncertain" if _uncertain(error) else "failed")
        if status == "uncertain" and current_action.verify is not None:
            try:
                if await current_action.verify() is True:
                    status = "completed"
            except Exception:
                LOGGER.exception("Agent action verification failed: run=%s step=%s", run_id, index)
        outcome = {"error_class": type(error).__name__, "permission": current_action.permission}
        output.step_outcomes[index] = (status, outcome)
        try:
            recorded = await asyncio.to_thread(
                self.repository.finish_step, run_id, index,
                owner=owner, status=status, outcome=outcome,
            )
        except Exception:
            LOGGER.exception("Agent action outcome could not be recorded: run=%s step=%s",
                             run_id, index)
            recorded = False
        if not recorded:
            status = "uncertain"
            output.step_outcomes[index] = (status, outcome)
        LOGGER.exception("Agent action failed: run=%s step=%s", run_id, index)
        return status

    async def _post_report(
        self, run_id, context, channel, progress, output: ActionRunOutput,
    ) -> None:
        run = await asyncio.to_thread(self.repository.run, run_id)
        for index, (status, outcome) in output.step_outcomes.items():
            run["steps"][index] = {**run["steps"][index], "status": status,
                                   "outcome_json": json.dumps(outcome)}
        if (progress is not None and not getattr(progress, "preserves_other_text", False)
                and self._only_posted_here(run, output)):
            try:
                await progress.delete()
            except discord.DiscordException:
                LOGGER.warning("Agent action progress could not be removed: run=%s", run_id)
            else:
                await self._call_on_finish(context, run, None, run_id)
                return
        report = self._report(run)
        chunks = chunk_response(report) or [report]
        previous = output.private_result
        private_view = (PrivateResultView(
            context.member.id,
            (*previous.parts, *output.private_parts) if previous else tuple(output.private_parts),
            (*previous.attachments, *output.private_files)
            if previous else tuple(output.private_files),
            panels=previous.panels if previous else (),
            panel_labels=previous.panel_labels if previous else (),
        ) if previous or output.private_parts or output.private_files else None)
        reported = None
        try:
            if progress is not None:
                if getattr(progress, "preserves_other_text", False):
                    remaining = await progress.replace_report(report, private_view)
                else:
                    await progress.edit(content=chunks[0],
                                        view=private_view if len(chunks) == 1 else None)
                    remaining = chunks[1:]
                reported = progress
                for index, chunk in enumerate(remaining):
                    reported = await channel.send(
                        chunk,
                        view=private_view if index == len(remaining) - 1 else None,
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
        await self._call_on_finish(context, run, reported, run_id)

    @staticmethod
    def _only_posted_here(run, output: ActionRunOutput) -> bool:
        """Posts in the run's own channel already show that every step finished."""
        return (not output.private_parts and not output.private_files and not output.private_result
                and not output.public_details
                and output.posted_here == len(run["steps"])
                and all(step["status"] == "completed" for step in run["steps"]))

    async def _call_on_finish(self, context, run, reported, run_id) -> None:
        if self.on_finish is not None:
            try:
                await self.on_finish(context, run, reported)
            except Exception:
                LOGGER.exception("Agent action outcome could not be retained: run=%s", run_id)


    _report = staticmethod(format_run_report)

    @staticmethod
    async def _send_parts(channel: Any, value: str) -> None:
        for part in chunk_response(value):
            await channel.send(part, allowed_mentions=discord.AllowedMentions.none())

    async def recover(self) -> None:
        try:
            wait_ready = getattr(self.bot, "wait_until_ready", None)
            if callable(wait_ready):
                await wait_ready()
            await asyncio.to_thread(self.repository.interrupt_incomplete,
                                    guild_id=self.guild_id)
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
