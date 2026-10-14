# Ranked snapshots

The collector reads every linked account's player profile and
`/players/{playerTag}/battlelog` through the application's ClashClient, including
accounts outside the family clans. It starts after boot and writes
`ranked_stats/ranked.sqlite3` under the application's data root. It needs no new
settings. Profile and log requests fail independently. Missing or invalid battle
identities are skipped and logged, and their polls are marked partial.

## Battle-log evidence and retention

The [official Swagger UI](https://developer.clashofclans.com/api-docs/index.html)
still requires signed-in cookies to load its schema. The
[community transcription of the official endpoint](https://github.com/MasiaAntoine/clash-of-clan-api-doc-official/blob/en/API/players/battlelog.md)
lists the route, but is not a Supercell-hosted source. Independent client source
confirms support:

- [clashy.go's client](https://github.com/ClashKingInc/clashy.go/blob/main/client.go)
  implements `GetBattleLog` against the official API base URL. Its
  [battle model](https://github.com/ClashKingInc/clashy.go/blob/main/battle_logs.go)
  exposes `battleTimestamp`, `attack` and `opponentPlayerTag`; its
  [enums](https://github.com/ClashKingInc/clashy.go/blob/main/enums.go) distinguish
  `HOME_VILLAGE`, `RANKED` and `LEGEND`.
- [Upstream coc.py's HTTP client](https://github.com/mathsman5133/coc.py/blob/master/coc/http.py)
  has no player battle-log wrapper in the inspected master revision. That older
  client's omission does not contradict the newer route.

No inspected source promises a fixed number of retained battles. Polling starts
at sixty seconds, then uses each response's **total** entry count `N` and time span
`S`: estimated turnover window `S * N / (N - 1)`, divided by four, bounded to
15–300 seconds. Farming and defenses count toward turnover because they can
displace ranked entries. Empty logs retain the startup interval; a single entry
or missing timestamps use fifteen seconds. The loop uses the shortest interval
among linked accounts. This adapts to the observed rolling log, without inventing
a fixed capacity or promising capture of an unseen burst. Each poll stores its
count, oldest/newest timestamps, chosen interval, failures and skipped entries.

The `ranked_battles` table retains every valid `RANKED` and `LEGEND` entry, including
defenses. A unique key combines account, UTC battle timestamp, mode, direction
and opponent. Repeated polls and restarts do not add another battle; changed
results or names update the saved payload. First-observed member/clan associations
remain unchanged on replay. They describe collection-time associations, not
verified ownership or clan membership at the historical battle time.

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
| `legendStatistics.currentSeason`, `.previousSeason`, `.bestSeason`: `id`, `rank`, `trophies` | Optional season observations, retained with their season IDs. |
| `tag`, `name`, `clan.tag` | Account identity, display name and observed clan; Discord ownership comes from AccountLinks at collection time. Absence of `clan` is allowed. |

The live reader already handles the first two groups. `leagueTier` and
`legendStatistics` are additional optional schema assumptions. A renamed field
does not silently become a zero or an alias: its known path is skipped and logged.

`read_ranked_stats` accepts exactly one of member IDs, account tags or clan
tags/codes, plus an ISO window of at most ninety days. Clan and member selectors
use ownership/membership at observation time. Results page through accounts in
last-observed trophy order, with tied selection ranks; these are ranks within
the chosen accounts. Any API-provided season rank stays in its original object.

Daily attacks used now count saved entries whose `attack` is true, including
zero-star losses, on the UTC date of `battleTimestamp`. Defenses have a separate
count. Late collection can backfill earlier days. Account/member/clan selections
include stored battles even when the requested window has no player snapshots.
Days without dated records are omitted. Counts describe observed records;
`counts_complete` and `coverage_complete` remain false because collection can
start late or miss entries from a rolling log. Unavailable logs do not erase
records or become zero attacks, and `attackWins` is no longer used to infer usage.

The available-attack quota remains null. Before deploying, verify the player-field
assumptions above and the battle log's `battleType`, boolean `attack`,
`opponentPlayerTag` and timezone-bearing `battleTimestamp` against host responses.
Entries missing an identity field cannot be safely dated or de-duplicated, and are
reported as skipped rather than assigned the poll date. No authenticated host
response or fixed-capacity guarantee was available during this change.
