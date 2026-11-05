# Saved data

Local runtime data lives under `data/` and is excluded from Git. Some paths
retain older feature names because existing deployments depend on them.

| Feature | Path | Contents |
|---|---|---|
| Application | `data/.avatar/` | Avatar file and upload state |
| Application | `data/.logs/` | Runtime logs |
| Application | `data/backups/` | Latest checked SQLite copies, made daily at 04:00 UTC with the same paths as under `data/` |
| Shared exports | `data/.exports/` | Temporary workbooks and crash leftovers |
| Family database | `data/family/family.sqlite3` | People and account ownership, when each member joined and left the Discord server, their roles, and when and in which channel they last wrote (never what they wrote); clan membership and donations; profiles and player history; regular, friendly and CWL wars with lineups and attacks; ranked battles, seasons, groups and when each battle log was checked; raid weekends with each member's attacks; time each account spent in each clan; the result of each data check |
| Shared history | `data/events/events.sqlite3` | What happened to things like news posts and clan transfers, and their open requests |
| Account links | `data/clan_links/links.sqlite3` | Suggestions and ignored tags |
| Achievements | `data/achievements/achievements.db` | Achievements, activity, coins, raffles, and rewards |
| Agent | `data/agent/agent.sqlite3` | Conversations and reply IDs used to continue after a restart |
| Agent | `data/agent/transcripts.sqlite3` | Request and reply text, plus troubleshooting records |
| Agent | `data/agent/research_jobs.sqlite3` | Progress and results for Discord searches and history reads |
| Agent | `data/agent/actions.sqlite3` | Action history, progress, tracked messages, scheduled requests, watched conditions, member timezones, and memes the bot posted (30 days, up to 500 per server) |
| Agent | `data/agent/gifs/` | Local image files for the meme library; copy on deploy |
| Agent | `data/agent/knowledge/*.md` | [Community reference files](../operators/agent-knowledge.md) |
| Health reports | `data/clan_health/settings.sqlite3` | Each clan's health-report expectations and their change log |
| Health reports | `data/clan_health/player_health_config.json` | Default health-report expectations |
| Clan reports | `data/clan_data/clan_activity.json` | Monthly report and board state |
| Clan transfers | `data/events/events.sqlite3` | Open transfer requests, their board and ping messages, and what happened to each request |
| CWL | `data/cwl/cwl_threads.json` | Registered CWL threads |
| CWL | `data/cwl/cwl_scheduler_state.json` | Reminder and schedule state |
| CWL | `data/cwl/cwl_transfer_state.json` | Transfer hub message ID, released roster cycles, and reminder state |
| CWL | `data/cwl/cwl_dashboard_state.json` | Prep and stars dashboard message IDs |
| CWL | `data/cwl/cwl_router_state.json` | CWL war rosters, board message IDs, and missed-attack state |
| CWL | `data/cwl/cwl_bonus_config.json` | Bonus rules |
| CWL | `data/cwl/cwl_bonus_config.backup.json` | Previous bonus rules |
| CWL | `data/cwl/cwl_bonus_config_audit.json` | Bonus-rule audit history |
| CWL | `data/cwl/cwl_bonus_dashboard_state.json` | Bonus board message IDs, clan status, and reward recipients |
| Events | `data/event_stats/event_stats.json` | Event definitions, order, and messages |
| Examinations | `data/examination/examination_state.json` | Examiners, cases, availability, panel, and cleanup state |
| Hibernation | `data/events/events.sqlite3` | Saved roles, hibernating members and their history |
| Leadership news | `data/events/events.sqlite3` | Pending publish prompts and what was published |
| Member lifecycle | `data/events/events.sqlite3` | Where each member came from when they joined and why they left, this week's join count per source, reports on applicants who have waited too long, and which ticket logs belong to whom |
| Old records | `data/records/records.sqlite3` | Leadership records, kept while that feature is rethought |
| Recruitment | `data/events/events.sqlite3` | Active trials, reminders, ticket activity and decisions |
| Recruitment | `data/recruitment/applicant_messages.json` | Application-summary message IDs, channels, and timestamps |
| Role connections | `data/role_connections/role_connections.json` | Role rules and board state |
| Rosters | `data/rosters/rosters.sqlite3` | Rosters, signups, posts, and schedules |
| Support and hibernation | `data/events/events.sqlite3` | Bot-created support and reactivation tickets and their history |
| Wars | `data/war/war_cache.json` | Processed wars, board and summary message IDs, and war-role lineups |

Features read shared data from the family database. Health reports read their
data there and saves only its own expectations. Achievements,
rosters, CWL state and rules, and other feature JSON remain feature-owned.

Agent cleanup deletes action history 90 days after execution, action progress
90 days after its last update, and records of tracked messages 90 days after
creation.
Saved requests, watched conditions, and member timezones stay saved.
See [Agent](../developers/agent.md#conversation-state) for conversation and
research job expiry, and [Privacy and AI](../users/privacy-and-ai.md#agent)
for how long request text and troubleshooting records are kept.
