# Package map

## Shared packages

| Package | Responsibility |
|---|---|
| `elbow_helper.app` | Startup, shared services, Discord connection, and shutdown |
| `elbow_helper.core` | Settings, paths, logs, lifecycle, and global errors |
| `elbow_helper.configuration` | Community guild, channel, role, clan, emoji, timezone, and style values |
| `elbow_helper.infrastructure.discord` | Shared interaction, view, pagination, embed, and command-group code |
| `elbow_helper.domain` | Shared player-tag handling, timezone input, and CWL date checks |
| `elbow_helper.data.collectors` | Collects family data from the Clash API, through the shared client, and from Discord member events |
| `elbow_helper.data.store` | Family schema, versioned upgrades and writes |
| `elbow_helper.data.reads` | Shared people, membership, profile, war, CWL, ranked and raid facts, and what data is missing |
| `elbow_helper.data.links` | Changes to who owns which account, saved all at once |
| [`elbow_helper.agent`](agent.md) | Questions, conversations, community references, and confirmed actions |
| `elbow_helper.infrastructure.ai` | Text-generation interface and application-owned client |
| `elbow_helper.infrastructure.clash` | Clash HTTP client |
| `elbow_helper.infrastructure.exports` | Excel generation, cleanup, and Google Sheet exports |
| `elbow_helper.infrastructure.persistence` | Atomic JSON and SQLite primitives |
| `elbow_helper.infrastructure.time` | UTC clocks, timezone resolution, and fixed offsets |

## Features

| Package | Responsibility |
|---|---|
| `account_links` | Link commands, suggestions and boards, reading the family database |
| `achievements` | Achievements, coins, inventory, rewards, and raffle |
| `attack_plans` | Attack-planning requests |
| `clan_health` | Health analysis and settings; activity, raids, wars and clan history read from the family database |
| `clan_reporting` | Monthly war and missing-Elder reports |
| `clan_transfers` | Family-clan transfer queues |
| `cwl` | CWL threads, rosters, transfers, boards, and bonuses reading saved league and war data |
| `diagnostics` | Ping and Clash status checks |
| `event_stats` | Scheduled participation trackers |
| `examination` | Promotion-examination intake, routing, and follow-up |
| `help` | Role-filtered command catalogue |
| `hibernation` | Saved roles, hibernation, return tickets, and transcripts |
| `leadership_news` | Publish and dismiss flow for leadership news |
| `member_lifecycle` | Joins, departures, invite attribution, and recruitment reports |
| `message_automation` | Configured replies and reactions |
| `recruitment` | Applicants, acceptance, trials, messages, and AI assistance |
| `role_connections` | Role rules based on other roles |
| `rosters` | Signup rosters, schedules, posts, roles, and exports |
| `support_tickets` | Support-ticket creation, closing, and transcripts |
| `wars` | Boards, summaries, roles, and statements reading saved war data |
