# Commands and access

This is the complete slash-command list. The wording below matches the
implemented `/help` catalogue. Run `/help command:<command>` in Discord for
current options, examples, notes, and restrictions.

## Access groups

`Member` means `/help` lists the command for the general community. Discord
channel permissions also apply.

- `Lead`: members with the Co-Leader or Cross-Leader role.
- `Lead Plus`: Lead plus Co-Leader Applicants.
- `Core leadership`: members with the Core Leadership or Core Senate role.
- `Recruiter`: members with the Main Recruiter, Recruiter, or Recruiter Intern
  role.
- `CWL helper`: members with the general CWL Helper role or either CWL
  leadership role.

## General

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/help` | Browse commands or get help using one. | Member |
| `/ping` | Check the bot's response time. | Member |
| `/plan` | Get help planning an attack. | Member |
| `/transfer request` | Ask to move to another family clan. | Member |
| `/transfer cancel` | Cancel your transfer request. | Member |

## Achievements & Economy

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/achievements` | Check achievements, progress, and rewards. | Member |
| `/achievement leaderboard` | See which members have earned the most achievements. | Member |
| `/inventory` | Check your coins and raffle ticket. | Member |
| `/economyinfo` | See how coins, tickets, and rewards work. | Member |
| `/grant coins` | Add coins to a member's balance. | Lead Plus |
| `/grant ticket` | Give a raffle ticket to a member. | Lead Plus |
| `/coinlog` | Check a member's coin history. | Core leadership |
| `/raffle list` | See who has raffle tickets right now. | Lead Plus |
| `/raffle draw` | Draw raffle winners for this month or an earlier month. | Lead Plus |
| `/raffle reroll` | Draw this month's raffle winners again. | Lead Plus |
| `/raffle history` | See the raffle prize and winners for a selected month. | Lead Plus |
| `/raffle prize` | Choose the prize shown in this month's raffle posts. | Core leadership |
| `/raffle remove` | Remove a member's raffle ticket. | Core leadership |
| `/raffle clear` | Clear this month's raffle winners. | Core leadership |
| `/achievement award` | Give an achievement to a member. | Lead |
| `/achievement remove` | Remove an achievement from a member. | Lead |

## Recruitment

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/accept` | Accept an applicant, link their accounts, and start their trial. | Recruiter or Lead |
| `/account add` | Link one or more Clash accounts to a Discord member. | Recruiter or Core leadership |
| `/account remove` | Unlink one or more Clash accounts. | Recruiter or Core leadership |
| `/account list` | Find who an account is linked to, or see a member's linked accounts. | Recruiter or Core leadership |
| `/checkup` | Start the recruitment conversation with an applicant and include any remaining setup steps. | Recruiter or Core leadership |
| `/finalize` | End a recruit's trial and give them full member access. | Recruiter or Core leadership |
| `/decline` | Decline an applicant and send the decision. | Recruiter or Core leadership |
| `/opinion` | Get an AI second opinion on an applicant ticket. | Recruiter or Core leadership |
| `/recstatements` | Send an Under 16 or Hyperactive message to an applicant. | Recruiter or Core leadership |
| `/recstats` | See how many applicants came from each recruitment source this week. | Recruiter or Core leadership |

## CWL

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/roster announcement` | Share CWL roster and transfer deadlines with the clan family. | Lead |
| `/cwl brief` | Post the clan's CWL rules, rotations, and leadership details. | Lead Plus or CWL helper |
| `/transfer reminder` | Notify members who still need to move to their CWL clan. | Lead Plus or CWL helper |
| `/cwl cc` | Record the Clan Castle status for a CWL day. | Lead Plus or CWL helper |
| `/cwl bonus` | Build a spreadsheet to help assign CWL bonus medals. | Lead Plus or CWL helper |
| `/cwl roster` | Build a workbook to help assign CWL signups to clans. | Lead Plus |
| `/cwl register` | Connect a clan's CWL thread to its status updates. | Core leadership |

## Rosters

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/roster create` | Set up a roster members can join with linked Clash accounts. | Lead Plus |
| `/roster edit` | Update a roster's name, clan, signup role, Town Hall minimum, or account limit. | Lead Plus |
| `/roster timing` | Set one opening and closing window for a roster. | Lead Plus |
| `/roster schedule` | Set automatic monthly opening and closing times for a roster. | Lead Plus |
| `/roster post` | Post a roster in this channel. | Lead Plus |
| `/roster export` | Export current roster signups to Google Sheets. | Lead Plus |
| `/roster list` | See current rosters and their timing. | Lead Plus |
| `/roster clone` | Create a roster using another roster's settings. | Lead Plus |
| `/roster delete` | Permanently remove a roster and its signup history. | Lead Plus |

## Reports & Exports

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/record add` | Document an incident involving a member. | Lead Plus |
| `/record export` | Download all leadership records or one member's records. | Lead Plus |
| `/record edit` | Update the category, type, or details of a member's record. | Lead Plus |
| `/record remove` | Delete one of a member's incident records. | Lead Plus |
| `/health player` | Export a health report for a Clash account. | Lead Plus |
| `/health clan` | Export a clan or family health report. | Lead Plus |
| `/health settings` | Set the activity expectations used in a clan's health reports. | Lead Plus |

## Events

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/event panel` | Manage event schedules and participation trackers. | Lead |
| `/event list` | See every event tracker and its current status. | Lead |
| `/event update` | Update every event count now. | Lead |

## War

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/warstatement one-attack-missed` | Ask players why they used only one war attack. | Lead Plus |
| `/warstatement missed-attacks` | Tell players they were removed from war after missing both attacks. | Lead Plus |
| `/warstatement first-claim` | Ask a player why they attacked someone else's first claim. | Lead Plus |
| `/warstatement breaking-rules` | Tell players they were removed from war for rule or communication problems. | Lead Plus |
| `/warstatement war-filler` | Tell war fillers why they were added and how future war selection works. | Lead Plus |

## Support & Access

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/open` | Open a support ticket for a member. | Lead |
| `/close` | Close the current ticket. | Recruiter or Lead |
| `/hibernate` | Save a member's roles and move them into hibernation. | Lead Plus |
| `/reactivate` | Restore your saved roles and return from hibernation. | Member |
| `/connections` | Manage rules that add or remove member roles. | Lead |

## Admin & Debug

| Command | What it does | [Access](#access-groups) |
|---|---|---|
| `/api` | Check Clash API status for a clan. | Core leadership |
