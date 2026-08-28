# Saved data

Local runtime data lives under `data/` and is excluded from Git. Some paths
retain older feature names because existing deployments depend on them.

| Feature | Path | Contents |
|---|---|---|
| Application | `data/.avatar/` | Avatar file and upload state |
| Application | `data/.logs/` | Runtime logs |
| Shared exports | `data/.exports/` | Generated workbooks awaiting cleanup |
| Account links | `data/clan_links/links.sqlite3` | Account links, suggestions, and current clan locations |
| Achievements | `data/achievements/achievements.db` | Achievements, activity, coins, raffles, and rewards |
| Clan health | `data/clan_health/clan_health.db` | Snapshots, activity, movement, wars, and report evidence |
| Clan health | `data/clan_health/player_health_config.json` | Health-report expectations |
| Clan reports | `data/clan_data/clan_activity.json` | Monthly report and board state |
| Clan transfers | `data/clan_transfers/transfer_queue.json` | Member transfer queues |
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
| Hibernation | `data/hibernation/hibernation.json` | Saved roles and hibernating members |
| Member lifecycle | `data/snapshot_intel/snapshot_intel.json` | Invites, ticket index, departures, and reports |
| Records | `data/records/records.sqlite3` | Leadership incident history |
| Recruitment | `data/recruitment/trial_data.json` | Active trials |
| Recruitment | `data/recruitment/trial_reminders.json` | Trial reminders |
| Recruitment | `data/recruitment/ticket_last_activity.json` | Recruitment ticket activity |
| Recruitment | `data/recruitment/applicant_messages.json` | Application-summary message IDs, channels, and timestamps |
| Role connections | `data/role_connections/role_connections.json` | Role rules and board state |
| Rosters | `data/rosters/rosters.sqlite3` | Rosters, signups, posts, and schedules |
| Support and hibernation | `data/support/created_tickets.json` | Bot-created support and reactivation tickets |
| Wars | `data/war/war_cache.json` | Processed wars, board and summary message IDs, and war-role lineups |
