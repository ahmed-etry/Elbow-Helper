# Scheduled work

The times below are how often the bot normally checks, not promises of when
something happens. Every 30 seconds the bot looks for data checks that are
due. A check that fails is saved as a failure, never as an empty result.

| Data the bot collects | Normal timing |
|---|---|
| Clan members and details | Every 15 minutes |
| Player profiles and battle logs | Hourly for family and linked accounts, and whenever a feature needs a more recent profile |
| Wars and CWL | Current wars and league groups about every 5 minutes; CWL wars every minute; more often near the end of a war; war logs daily |
| Ranked history | League history daily; active ranked groups every 6 hours and after the season ends |
| Raids and game lists | Raid weekends hourly; the Gold Pass season and league and label lists daily |
| Discord | Joins, leaves, and role changes as they happen, plus a full member check at startup and daily; last-message times saved every 5 minutes |

| Feature | Work and normal timing |
|---|---|
| Account links | Update which clan each account is in and the missing-Elder boards every 15 minutes, from saved clan data |
| Achievements | Check time achievements, salaries, and members who left daily; refresh the raffle hub hourly |
| Clan reports | Check for the previous month's war reports every 30 minutes, due from day 1 at 08:00 UTC |
| Clan transfers | Remove expired requests every 5 minutes |
| CWL | Check reminders and war results every minute; refresh dashboards every 20 minutes; open discussion threads daily during the discussion window; check monthly thread cleanup daily from day 13 UTC |
| Events | Refresh every 5 minutes, or every minute when an event starts or ends within a day |
| Examinations | Sort tickets every 15 minutes, delete outdated posts in the examination room every 30 minutes, and check follow-ups every 10 minutes |
| Recruitment reports | Check daily for weekly recruitment reports and overdue applicants |
| Recruitment | Sort tickets every 15 minutes; check unanswered messages and remove old reminders every 30 minutes; check trials hourly; remove old application summaries every 6 hours |
| Rosters | Check opening and closing schedules every minute |
| Wars | Update after each family war check; remove summaries after 48 hours, checked hourly |
| Agent | Check reminders, saved requests, and watches that members set up through the agent every 30 seconds |
| Database backups | Copy and check each database daily at 04:00 UTC, and after startup if a copy is missing or over a day old |

CWL monthly cleanup removes discussion messages and the prep status board,
resets Clan Castle status, and archives registered threads. It keeps the first
message, the thread's status post, and pinned messages. When members' messages
bury the status post, the bot posts it again at the bottom.
