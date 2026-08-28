# Architecture

## Boundaries

Feature-specific Discord handlers, rules, saved data, scheduled work, and
user-facing output stay under `elbow_helper.features`.

Shared packages have narrower jobs:

- `configuration`: guild, channels, roles, clans, emoji, timezones, and style;
- `core`: startup, settings, paths, logging, lifecycle, and app-wide errors;
- `discord`: interaction helpers, views, pagination, embeds, and shared command
  registration;
- `domain`: shared player-tag handling, timezone input, and CWL date checks;
- `infrastructure`: shared clients and utilities for the Clash API, AI text
  generation, spreadsheets, time, and storage.

Features share data through services or read-only interfaces. The feature that
creates a database or state file is the only one that accesses it directly.

Important current dependencies include:

```text
Account Links -> Recruitment, Records, Rosters, CWL, Wars, Clan Reporting
Achievements -> Recruitment, Hibernation, CWL
Clan Health -> Attack Plans, CWL
Records -> CWL
Rosters -> CWL and Wars
Hibernation -> Member Lifecycle
```

## Shared services

`elbow_helper.app` loads process settings and creates shared services before
feature setup begins.

All Clash requests use the application-owned
`elbow_helper.infrastructure.clash.ClashClient`. Player-tag normalization and
URL encoding use `elbow_helper.domain.player_tags`.

## Shared command groups

The registry assembles `/cwl`, `/roster`, `/transfer`, and `/record` from
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
- Multi-step role and ticket workflows must report partial completion instead
  of presenting the whole action as successful.

See [Package map](package-map.md) for feature ownership and
[Feature loading and restarts](extension-lifecycle.md) for lifecycle rules.
