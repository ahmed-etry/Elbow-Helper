# Rosters, CWL, and transfers

## Rosters

1. `/roster create` sets the name, clan, signup role, and account limit.
2. `/roster edit` changes the name, clan, signup role, Town Hall minimum, or
   account limit.
3. `/roster timing` sets one opening and closing window. `/roster schedule`
   creates a monthly schedule.
4. Run `/roster post` in the channel where the roster should appear.
5. `/roster list` shows every roster's status, limits, and timing.
6. `/roster export` creates a spreadsheet with the selected roster's current
   signups.

Members join the roster with linked Clash accounts.
[Lead Plus](../../reference/commands-and-permissions.md#access-groups) can
manage individual signups through the roster controls. A Town Hall minimum
applies to new signups and does not remove accounts already on the roster.

`/roster clone` copies a roster's settings without its signups. `/roster delete`
permanently removes the roster and its signup history after confirmation.

## Timing

`/roster timing` sets a one-time window. Enter full opening and closing dates
and times, then choose the timezone used for both.

`/roster schedule` repeats every month. Choose day `1`–`28`, `last` for the
final day, `last-1` for the day before, or `last-2` for two days before.

Monthly schedules keep the same UTC time. If local clocks later change for
daylight saving, the roster's local opening and closing times can shift.

## CWL

- `/cwl register` connects a clan to its CWL discussion thread and adds its
  status post.
- `/roster announcement` previews and posts the clan-family roster and transfer
  deadlines.
- `/cwl roster` builds a workbook to help assign CWL signups to clans.
- `/cwl brief` posts the clan's CWL rules, rotations, and leadership details.
- `/cwl bonus` builds a workbook to help assign CWL bonus medals.

## Transfers

Members use `/transfer request` to join a family clan's queue and
`/transfer cancel` to leave it. `/transfer reminder` notifies members who still
need to move to their assigned CWL clan. Once the in-game moves are complete,
select **Transfers Done — Clear Queue** on the relevant queue.
