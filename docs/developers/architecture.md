# Architecture

## Boundaries

Feature-specific Discord handlers, rules, saved data, scheduled work, and
user-facing output stay under `elbow_helper.features`.

Shared packages have narrower jobs:

- `configuration`: guild, channels, roles, clans, emoji, timezones, and style;
- `core`: startup, settings, paths, logging, lifecycle, and app-wide errors;
- `infrastructure.discord`: interaction helpers, views, pagination, embeds, and shared command
  registration;
- `domain`: shared player-tag handling, timezone input, and CWL date checks;
- `data`: family database collectors, storage, ownership changes, and shared
  reads for features and the agent;
- `infrastructure`: shared clients and utilities for the Clash API, AI text
  generation, spreadsheets, time, and storage.

Features share data through services or read-only interfaces. `data/store`
owns the family schema and writes; `data/reads` returns facts and says what data is missing
from one consistent read, without calling the Clash API or reading features' own files.
Code using it keeps unknown values unknown and never counts missing data as
inactivity. Each feature's own files are read only through that feature.

Important current dependencies include:

```text
Family database -> Account Links, Wars, CWL, Clan Health, Clan Reporting, Agent
Account Links -> Recruitment, Rosters, CWL, Wars, Clan Reporting
Achievements -> Recruitment, Hibernation, CWL
Clan Health -> Attack Plans, CWL
Rosters -> CWL and Wars
Hibernation -> Member Lifecycle
```

## Shared services

`elbow_helper.app` loads process settings once and creates shared services before
feature setup begins.

All Clash requests use the application-owned
`elbow_helper.infrastructure.clash.ClashClient`. Player-tag normalization and
URL encoding use `elbow_helper.domain.player_tags`.

Only the family collectors ask the Clash API for data the family database holds. War and CWL features make no Clash API
calls for wars or league groups; their boards, roles, reports and scoring read
the family database. Ranked history also comes from there, without asking the Clash API for
missing battles. Health reports read the family database as well. Only the agent's
live Clash API lookup asks the Clash API itself, through the same shared client.

Family database work runs on background threads through
`FamilyStore.call` or `asyncio.to_thread`.

Spreadsheet features share the application-owned `GoogleSheetsPublisher`,
`WorkbookWriter`, and `LocalExportStore`. Features define report contents;
infrastructure owns Drive retention and temporary-file cleanup.

## Shared command groups

The registry assembles `/cwl`, `/roster`, and `/transfer` from
handlers owned by their features. It loads after those features so each group
is built from their handlers.

## Compatibility

Keep these public and saved interfaces unchanged during code refactors:

- slash-command or option names;
- component IDs used by existing Discord messages;
- database and JSON paths, versions, or fields;
- report filenames, sheets, or columns.

Changes to saved data paths or fields need a migration. Changes to permanent
button or menu IDs need a transition for existing Discord messages.

## Workflow invariants

- Incomplete Clash responses must not erase the last complete account
  locations, clan boards, or live-war state.
- War and roster cleanup must keep a shared role while either workflow still
  requires it.

See [Package map](package-map.md) for feature ownership and
[Feature loading and restarts](extension-lifecycle.md) for lifecycle rules.
