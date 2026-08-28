# Wars, health reports, and records

## Wars

War boards track each clan's regular war, showing the opponent, war state,
score, attacks remaining, destruction, and Town Hall breakdown for both clans.
When the war ends, leadership receives a summary with the result and missed
attacks. The bot also gives the clan's war role to linked members in the live
lineup and removes it after the war when no roster still needs it.

The `/warstatement` commands send follow-up messages for:

- one missed attack;
- both attacks missed;
- attacking another player's first claim;
- war-rule or communication problems;
- war fillers.

## Health reports

`/health player` creates a health report for a Clash account. `/health clan`
summarizes member health across a clan or the full family. Both reports cover
wars, CWL, Raid Weekends, Clan Games, donations, and progression. The player
report also includes clan history. `/health settings` changes the expectations
used to evaluate future reports.

Reports depend on history collected during the selected dates. The default
period is 30 days, and the command also accepts custom dates.

## Leadership records

`/record add`, `/record edit`, and `/record remove` maintain private incident
history for a Discord member. `/record export` downloads all records or one
member's records.

## War summaries and Elder reminders

Each month, clan data threads receive a chart showing the previous month's war
wins, ties, losses, and win rate. Clan leadership channels also keep a board
showing which members still need to be promoted back to Elder in-game.
