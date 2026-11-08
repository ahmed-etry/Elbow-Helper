# Architecture

`elbow_helper.app` reads process settings once and builds the shared clients,
family database readers, account-link service, workflow history, and Discord
publishing queue before loading features.

## Boundaries

| Package | Holds |
|---|---|
| `configuration` | Brown Elbow clans, roles, channels, emoji, and timezones |
| `core` | Startup, paths, settings, logging, background work, and shutdown |
| `domain` | Shared rules for tags, timezones, war attendance, and war views |
| `infrastructure` | Clients, file and database helpers, Discord utilities, and exports |
| `data` | Collecting, saving, and reading family data and workflow history |
| `features` | Commands, buttons, boards, reports, and each workflow's rules |
| `agent` | Conversations, lookups, answer checks, and changes members approve |

Shared packages do not import features or the agent. Features do not import
the agent. Modules in `agent/features/` call feature services for workflow
rules. To use another feature's data, go through that feature's service or
reader; don't open its files directly.

## Family database and workflow history

`data/collectors` fetches clan and player profiles, wars, CWL, ranked history,
and raids through the shared Clash client. It also records Discord membership,
roles, and the time and channel of each member's last message, without its text.
Features ask the collectors' profile service when they need a fresh clan or
player profile.

`data/store` defines the family database tables and does all writes.
`data/reads` answers each read from one read-only transaction, with when each
value was seen and what data is missing. It never fetches missing data or opens
feature files. Callers must not treat an unknown value as zero activity.

Account ownership changes go through `data.links.LinkService`.
`data.events.EventsStore` saves each workflow's current data together with its
dated events; feature services decide what those events mean. Achievements,
rosters, and other features keep their own data. See
[Data and migrations](data-and-migrations.md).

## Clash API and Discord output

Every Clash API request goes through the one shared
`infrastructure.clash.ClashClient` that the app creates. Use
`domain.player_tags` to clean up tags and encode them for URLs. War, CWL,
ranked-history, and health features read the family database. Agent lookups
that ask the Clash API directly, and `/api`, use the shared client.

Run blocking file and SQLite work on a background thread, through the store's
async method or `asyncio.to_thread`.

`infrastructure.discord.publishing.PublishingQueue` keeps only the latest
waiting edit for each message or channel, and the latest channel order. It
paces sending and retries failures. Commands and new messages go to Discord
directly.

Reports share `GoogleSheetsPublisher`, `WorkbookWriter`, and `LocalExportStore`.
Features decide what a report contains; infrastructure uploads it and cleans up
files. Shared war rules and views live in `domain`; Discord war boards and
emoji live in `infrastructure.discord`.

## Names that outlive the code

Slash commands, options, IDs of buttons that keep working after a restart,
saved file paths and fields, and report filenames and columns stay in use
after the Python code is renamed. Changing one needs approval and an upgrade
for existing data and messages.

A partial Clash API response must not wipe out account locations, boards, or
war data that were complete. When wars and rosters clean up, a role both use
stays until neither needs it.

See [Package map](package-map.md) for what each package does and
[Feature loading and restarts](extension-lifecycle.md) for startup order.
`tests/core/test_architecture_rules.py` checks which packages may import which,
where HTTP sessions and environment variables may be used, and how features
and commands are set up.
