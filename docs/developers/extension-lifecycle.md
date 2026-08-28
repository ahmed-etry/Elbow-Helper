# Feature loading and restarts

## Load order

`elbow_helper.core.lifecycle.REQUIRED_EXTENSIONS` defines startup order.
Features that provide services load before consumers. The command registry
loads last and assembles `/cwl`, `/roster`, `/transfer`, and `/record` from the
loaded feature cogs. Startup stops if any extension fails to load.

## Setup

Features live under `elbow_helper/features/<feature>/` and expose
`async setup(bot)` from `__init__.py`. Setup retrieves the services the feature
depends on, creates any feature-owned repositories or services, adds the cog,
and registers permanent views where needed.

Settings are loaded in `elbow_helper.app`. Feature modules must not read
environment variables at import time.

## Ready events and tasks

Discord may fire `on_ready` more than once. Startup work must be guarded so a
reconnect does not duplicate tasks, views, or messages.

Background tasks must wait for readiness, stop on unload, be safe to run more
than once, and limit temporary retries.

## Shutdown and updates

Production updates use a full process restart so startup rebuilds feature
dependencies and shared command groups in their declared order.

Features cancel tasks and close resources they create. The app closes Discord
and application-owned clients.
