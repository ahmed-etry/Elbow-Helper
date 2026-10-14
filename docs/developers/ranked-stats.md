# Ranked snapshots

The collector polls every linked account every five minutes through the application's
ClashClient, including accounts outside the family clans. It starts after boot and
writes `ranked_stats/ranked.sqlite3` under the application's data root. It needs no
new settings. Failed requests produce unavailable observations; missing or invalid
fields are skipped and logged once per account/field until the field returns.

## Player-field assumptions to check on the host

No authenticated player response was available during implementation. The
[official Swagger UI](https://developer.clashofclans.com/api-docs/index.html)
loads its schema URL and temporary token from signed-in cookies, so the current
official schema could not be inspected without an account session either.
The fields below are assumptions to verify against one host response before
deployment, rather than a claim that the current endpoint returns every field.

| Assumed field | Basis and use |
| --- | --- |
| `trophies`, `bestTrophies`, `attackWins`, `defenseWins` | Existing `read_clash` player fields; stored when nonnegative integers. |
| `league.id`, `league.name` | Existing player league object; retained as the legacy field. |
| `leagueTier.id`, `leagueTier.name` | Modern player league field in the [SDK's player model](https://github.com/ClashKingInc/clashy.go/blob/main/player.go); retained separately when present. |
| `legendStatistics.legendTrophies` | Optional Legend total; stored when present. |
| `legendStatistics.currentSeason`, `.previousSeason`, `.bestSeason`: `id`, `rank`, `trophies` | Optional season observations; season ID changes break counter intervals. |
| `tag`, `name`, `clan.tag` | Account identity, display name and observed clan; Discord ownership comes from AccountLinks at collection time. Absence of `clan` is allowed. |

The live reader already handles the first two groups. `leagueTier` and
`legendStatistics` are additional optional schema assumptions. A renamed field
does not silently become a zero or an alias: its known path is skipped and logged.

`read_ranked_stats` accepts exactly one of member IDs, account tags or clan
tags/codes, plus an ISO window of at most ninety days. Clan and member selectors
use ownership/membership at observation time. Results page through accounts in
last-observed trophy order, with tied selection ranks; these are ranks within
the chosen accounts. Any API-provided season rank stays in its original object.

`attackWins` counts wins, not all attempts, and its relationship to ranked-only
battles has not been verified. Positive differences between consecutive valid
observations are reported as observed wins. Missing observations, ownership/clan
changes and detected resets break intervals. Daily values include only intervals
whose endpoints fall on the same UTC date. Window edges, polling gaps and resets
can hide wins; a missing daily value is unknown, not zero.

Neither attack usage nor an available-attack quota can be established from these
fields. Both remain null. The implementation does not infer quotas from league
names or describe wins as ranked attacks. Before deploying, compare the assumed
paths above and the meaning of `attackWins` with a real player response on the host.
