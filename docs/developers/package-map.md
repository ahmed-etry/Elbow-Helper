# Package map

## Shared packages

| Package | What it does |
|---|---|
| `elbow_helper.app` | Startup, shared services, Discord connection, and shutdown |
| `elbow_helper.core` | Settings, paths, logs, feature loading, and errors no command handled |
| `elbow_helper.configuration` | Server, channel, role, clan, emoji, timezone, and style values |
| `elbow_helper.infrastructure.discord` | Shared interaction, view, pagination, embed, and command-group code |
| `elbow_helper.domain` | Player tags, timezone input, CWL dates, war attendance and war views |
| `elbow_helper.data.collectors` | Collects family data from the Clash API, through the shared client, and from Discord member events |
| `elbow_helper.data.store` | Family database tables, upgrades and writes |
| `elbow_helper.data.reads` | Lookups of people, membership, profiles, wars, CWL, ranked battles and raids, and what data is missing |
| `elbow_helper.data.links` | Changes to who owns which account, saved all at once |
| `elbow_helper.data.events` | Each workflow's current data and dated events, saved together |
| [`elbow_helper.agent`](agent.md) | Questions, conversations, community reference files, and changes members approve |
| `elbow_helper.infrastructure.ai` | The LLM client the app creates, and the interface features use to call it |
| `elbow_helper.infrastructure.clash` | Clash API client |
| `elbow_helper.infrastructure.exports` | Excel files, their cleanup, and Google Sheet exports |
| `elbow_helper.infrastructure.persistence` | JSON and SQLite helpers that save all or nothing |
| `elbow_helper.infrastructure.time` | UTC clocks, timezone lookup, and fixed offsets |

## Features

| Package | What it does |
|---|---|
| `account_links` | Link commands, suggestions and boards, reading the family database |
| `achievements` | Achievements, coins, inventory, rewards, and raffle |
| `attack_plans` | `/plan` attack-planning requests |
| `clan_health` | Health reports and settings; activity, raids, wars and clan history read from the family database |
| `clan_reporting` | Monthly war and missing-Elder reports |
| `clan_transfers` | Family-clan transfer queues |
| `cwl` | CWL threads, rosters, transfers, boards, and bonuses, from saved league and war data |
| `diagnostics` | `/ping` and Clash API status checks |
| `event_stats` | Event trackers |
| `examination` | Promotion tickets, matching examiners, and follow-ups |
| `help` | The `/help` command list, filtered by each member's roles |
| `hibernation` | Saved roles, hibernation, return tickets, and transcripts |
| `leadership_news` | Publish and Dismiss buttons for leadership news |
| `member_lifecycle` | Joins, departures, which invite each member used, and recruitment reports |
| `auto_reactions` | Automatic replies and reactions |
| `recruitment` | Applicants, acceptance, trials, messages, and AI help |
| `role_connections` | Role rules based on other roles |
| `rosters` | Signup rosters, schedules, posts, roles, and exports |
| `support_tickets` | Opening, closing, and transcripts for support tickets |
| `wars` | Boards, summaries, roles, and statements, from saved war data |
