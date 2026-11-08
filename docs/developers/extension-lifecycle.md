# Feature loading and restarts

## Load order

`elbow_helper.core.lifecycle.REQUIRED_EXTENSIONS` sets the startup order.
Features that provide services load before the features that use them. The
command registry loads last and builds `/cwl`, `/roster`, and `/transfer` from
the loaded feature cogs. Startup stops if any extension fails to load.

## Setup

Features live under `elbow_helper/features/<feature>/` and expose
`async setup(bot)` from `__init__.py`. Setup gets the shared services the
feature needs, creates the feature's own stores and services, adds the cog, and
registers buttons that must keep working after a restart.

Settings are loaded in `elbow_helper.app`. Feature modules must not read
environment variables at import time.

## Ready events and tasks

Discord may fire `on_ready` more than once. Guard startup work so a reconnect
does not repeat tasks, views, or messages.

Background tasks must wait until the bot is ready, stop when their feature
unloads, be safe to run more than once, and retry temporary failures only a
limited number of times.

Workflow history opens before extensions load. Family collectors start after
extensions load and wait for `boot_complete` before collecting.

## Shutdown and updates

Updates to the live bot need a full restart, so features and shared command
groups load again in their set order.

Each feature cancels the tasks and closes what it opened. The app closes
Discord and the shared clients.
