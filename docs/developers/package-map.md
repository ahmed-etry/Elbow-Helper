# Package map

## Shared packages

| Package | Responsibility |
|---|---|
| `elbow_helper.app` | Startup, shared services, Discord connection, and shutdown |
| `elbow_helper.core` | Settings, paths, logs, lifecycle, and global errors |
| `elbow_helper.configuration` | Community guild, channel, role, clan, emoji, timezone, and style values |
| `elbow_helper.discord` | Shared interaction, view, pagination, embed, and command-group code |
| `elbow_helper.domain` | Shared player-tag handling, timezone input, and CWL date checks |
| `elbow_helper.infrastructure.ai` | Text-generation interface and application-owned client |
| `elbow_helper.infrastructure.clash` | Clash HTTP client |
| `elbow_helper.infrastructure.exports` | Excel generation, cleanup, and Google Sheet exports |
| `elbow_helper.infrastructure.persistence` | Atomic JSON and SQLite primitives |
| `elbow_helper.infrastructure.time` | UTC clocks, timezone resolution, and fixed offsets |

## Features

| Package | Responsibility |
|---|---|
| `account_links` | Discord member and Clash account links |
| `achievements` | Achievements, coins, inventory, rewards, and raffle |
| `attack_plans` | Attack-planning requests |
| `clan_health` | Activity history, analysis, settings, and reports |
| `clan_reporting` | Monthly war and missing-Elder reports |
| `clan_transfers` | Family-clan transfer queues |
| `cwl` | CWL threads, rosters, transfers, boards, and bonuses |
| `diagnostics` | Ping and Clash status checks |
| `event_stats` | Scheduled participation trackers |
| `examination` | Promotion-examination intake, routing, and follow-up |
| `help` | Role-filtered command catalogue |
| `hibernation` | Saved roles, hibernation, return tickets, and transcripts |
| `leadership_news` | Publish and dismiss flow for leadership news |
| `member_lifecycle` | Joins, departures, invite attribution, and recruitment reports |
| `message_automation` | Configured replies and reactions |
| `records` | Private leadership records and exports |
| `recruitment` | Applicants, acceptance, trials, messages, and AI assistance |
| `role_connections` | Role rules based on other roles |
| `rosters` | Signup rosters, schedules, posts, roles, and exports |
| `support_tickets` | Support-ticket creation, closing, and transcripts |
| `wars` | War polling, boards, summaries, roles, and statements |
