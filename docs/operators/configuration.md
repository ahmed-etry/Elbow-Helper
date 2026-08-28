# Configuration

## Deployment credentials

[`.env.example`](../../.env.example) lists every deployment credential.

## Community settings

These files live under `elbow_helper/configuration`:

| File | Contains |
|---|---|
| `guild.py` | Server ID |
| `channels.py` | Channel, category, and thread IDs |
| `roles.py` | Access groups and managed role IDs |
| `clans.py` | Family clans, clan roles, channels, and routing |
| `emojis.py` | Application emoji refresh and retry timing |
| `style.py` | Default embed color and thumbnail |
| `timezones.py` | Region-role timezones |
| `files.py` | Support and hibernation ticket file |

Changes to these modules require a bot restart.

## Settings managed in Discord

Roster schedules, clan-health expectations, and CWL bonus rules are managed
through their commands and controls. [Saved data](../reference/storage.md)
shows where the bot keeps these settings.
