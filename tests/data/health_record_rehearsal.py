"""Test-only rehearsal of clan health moving every activity, player health and raid reader to the family record.

Three steps, on copies only; Clash, Discord and the LLM are never called:

``python <this file> capture <work folder> <result file> <source tree>``
    The work folder holds ``family.sqlite3`` (migrated, with the startup copies
    done) and ``clan_health.db``. The source tree's code builds, on fresh
    copies, every health workbook leadership can produce: the clan workbook for
    every clan and the whole family, and the player workbook for a fixed sample
    of accounts, for the last 30 days, three months, February to now and
    September (a period before the record's own rosters). The old tree reads
    the old file's stored rosters, snapshots and raid rows; the new tree reads
    only the record.

``python <this file> compare <work folder> <old result> <new result> <report file>``
    Counts every difference by period, sheet and field, and names its cause
    from both sources; what no rule explains is listed as unexplained.

Results keep account tags but never names.
"""

import asyncio
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

SECOND = 1_000_000
DAY = 86400
SAMPLE = 3
FIELDS = ("status", "townhall", "hero_delta", "pet_delta", "equipment_delta", "troop_delta", "spell_delta",
          "th_delta", "games_delta", "capital_delta", "donations", "donations_received", "raid_attacks", "raid_loot",
          "raid_weekends_joined", "raid_weekends_window", "war_events_joined", "war_hits_expected", "war_hits_used")


def moment(value):
    return datetime.fromtimestamp(value, tz=timezone.utc)


def periods(at):
    return {"last_30_days": (at - 30 * DAY, at), "three_months": (at - 92 * DAY, at),
            "february_on": (int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()), at),
            "september": (int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()),
                          int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()))}


def masked(sheet, column):
    return [[cell for index, cell in enumerate(row) if index != column] for row in sheet]


class Side:
    def __init__(self, folder, new):
        from elbow_helper.data.reads import WarReads
        from elbow_helper.data.store import FamilyStore
        from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
        from elbow_helper.features.clan_health.record_wars import RegularWars
        self.new, self.folder = new, folder
        store = FamilyStore(folder / "family.sqlite3")
        wars = WarReads(store)
        self.regular, self.league = RegularWars(wars), RegularWars(wars, types=("CWL",))
        if new:
            from elbow_helper.data.reads import ActivityReads, RaidReads, StayReads
            from elbow_helper.features.clan_health.database.schema import copy_old_settings
            from elbow_helper.features.clan_health.record import HealthRecord
            self.settings = folder / "settings.sqlite3"
            copy_old_settings(self.settings, folder / "clan_health.db")
            self.source = HealthRecord(ActivityReads(store), RaidReads(store), StayReads(store))
        else:
            from elbow_helper.features.clan_health.database import ClanHealthRepository
            self.settings = folder / "clan_health.db"
            self.source = ClanHealthRepository(self.settings)
        self.analyzer = ClanHealthAnalyzer(self.source, self.regular, self.league)

    def settings_patch(self):
        return patch("elbow_helper.features.clan_health.database.config_store.DB_PATH", self.settings)

    def commands(self, *, clash_configured):
        from elbow_helper.features.clan_health.commands import ClanHealthCommandMixin

        class Harness(ClanHealthCommandMixin):
            async def prepare_health_export(self, **workbook):
                return SimpleNamespace(**workbook)

        harness = Harness()
        harness.analyzer, harness.regular_wars, harness.league_wars = self.analyzer, self.regular, self.league
        # The old clan workbook refuses without a Clash connection; old player workbooks ask Clash only when one
        # is set up, so the old side's player workbooks run without one. Nothing here can reach Clash.
        harness.clash_client = SimpleNamespace(configured=clash_configured, get=None)
        harness.collector = None
        if self.new:
            harness.record = self.source
        else:
            harness.repository = self.source
        return harness

    def clan(self, code, start, end):
        harness = self.commands(clash_configured=True)
        rows = []
        original = self.analyzer.build_sheets

        def capture(**kwargs):
            sheets, all_rows, totals = original(**kwargs)
            rows.extend(all_rows)
            return sheets, all_rows, totals

        harness.analyzer = SimpleNamespace(build_sheets=capture)
        result = asyncio.run(harness.run_clan_health_export(
            code, mode="custom", date_from=moment(start).date().isoformat(), date_to=moment(end).date().isoformat()))
        if result.export is None:
            return {"status": result.status}
        return {"status": result.status,
                "rows": {row["player_tag"]: {key: row.get(key) for key in FIELDS} | {
                    "flags": sorted(row.get("flags") or []), "clan_code": row.get("clan_code")} for row in rows},
                "notes": [line for line in result.export.summary_lines if line.startswith("Notes:")],
                "sheets": {name: masked(sheet, 0 if name == "Activity" else 1) for name, sheet in result.export.sheets}}

    def player(self, tag, start, end):
        harness = self.commands(clash_configured=False)
        export = asyncio.run(harness.build_player_health_export(
            player_tag=tag, now=moment(end), window_mode="custom", season_key="rehearsal",
            trend_season_key="2026-10", cycle_start=moment(start), cycle_end=moment(end), window_label="rehearsal",
            date_from=moment(start).date().isoformat(), date_to=moment(end).date().isoformat()))
        if export is None:
            return None
        sheets = {}
        for title, sheet in export.sheets:
            if title == "Overview":
                sheet = [row if row[0] != "Player" else ["Player", "-", row[2]] for row in sheet]
            elif title in ("Wars", "CWL"):
                sheet = masked(sheet, 2)
            sheets[title] = sheet
        return sheets


def sample_tags(folder, at):
    """The old file's roster at the latest complete run before ``at``: a few accounts per clan."""
    with closing(sqlite3.connect(f"file:{(folder / 'clan_health.db').as_posix()}?mode=ro", uri=True)) as old:
        run = old.execute("SELECT run_id FROM report_runs WHERE scope='BACKGROUND_ALL' AND partial=0 AND created_ts<=? "
                          "ORDER BY created_ts DESC LIMIT 1", (at,)).fetchone()[0]
        rows = old.execute("SELECT clan_code,player_tag FROM report_players WHERE run_id=? ORDER BY clan_code,player_tag",
                           (run,)).fetchall()
    by_clan = defaultdict(list)
    for code, tag in rows:
        by_clan[code].append(tag)
    return sorted(tag for tags in by_clan.values() for tag in tags[:SAMPLE])


def capture(work, out):
    from elbow_helper.features.clan_health.config import CLAN_EXPORT_ORDER
    work = Path(work)
    new = importlib.util.find_spec("elbow_helper.features.clan_health.record") is not None
    folder = work / ("new" if new else "old")
    folder.mkdir(exist_ok=True)
    for name in ("family.sqlite3", "clan_health.db"):
        shutil.copyfile(work / name, folder / name)
    prepared = json.loads((work / "prepared.json").read_text(encoding="utf-8"))
    at = prepared["at"] // SECOND
    side = Side(folder, new)
    result = {"side": "new" if new else "old", "at": at, "periods": periods(at), "sample": sample_tags(folder, at),
              "clans": {}, "players": {}}
    with side.settings_patch():
        for name, (start, end) in periods(at).items():
            print(result["side"], name, flush=True)
            result["clans"][name] = {code: side.clan(code, start, end) for code in ("ALL", *CLAN_EXPORT_ORDER)}
            result["players"][name] = {tag: side.player(tag, start, end) for tag in result["sample"]}
    Path(out).write_text(json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return {"side": result["side"], "written": str(out)}


# Compare ---------------------------------------------------------------------------------


class Evidence:
    """What the old file and the record copy hold, to name each difference's cause."""

    def __init__(self, work):
        self.old = sqlite3.connect(f"file:{(Path(work) / 'clan_health.db').as_posix()}?mode=ro", uri=True)
        self.record = sqlite3.connect(f"file:{(Path(work) / 'family.sqlite3').as_posix()}?mode=ro", uri=True)

    def old_roster_time(self, end):
        row = self.old.execute("SELECT created_ts FROM report_runs WHERE scope='BACKGROUND_ALL' AND partial=0 "
                               "AND created_ts<=? ORDER BY created_ts DESC LIMIT 1", (end,)).fetchone()
        return row[0] if row else None

    def record_membership_start(self, tag):
        return self.record.execute("SELECT MIN(m.from_at) FROM clan_membership m JOIN accounts a ON a.id=m.account_id "
                                   "WHERE a.player_tag=?", (tag,)).fetchone()[0]

    def record_start(self):
        return self.record.execute("SELECT MIN(from_at) FROM clan_membership").fetchone()[0] // SECOND

    def old_last_capture(self, tag, end):
        return self.old.execute("SELECT MAX(captured_ts) FROM player_snapshots WHERE player_tag=? AND captured_ts<=?",
                                (tag, end)).fetchone()[0]

    def old_raids(self, tag, start, end):
        return self.old.execute("SELECT COUNT(*),COALESCE(SUM(attacks),0),COALESCE(SUM(loot),0) FROM raid_member_activity "
                                "WHERE player_tag=? AND end_ts>=? AND end_ts<=?", (tag, start, end)).fetchone()

    def record_raids(self, tag, start, end):
        return self.record.execute(
            "SELECT COUNT(*),COALESCE(SUM(l.attacks),0),COALESCE(SUM(l.capital_loot),0) FROM raid_member_lines l "
            "WHERE l.player_tag=? AND l.weekend_start+?>=? AND l.weekend_start+?<=?",
            (tag, 3 * DAY * SECOND, start * SECOND, 3 * DAY * SECOND, end * SECOND + SECOND - 1)).fetchone()

    def old_weekend_ends(self, start, end):
        return self.old.execute("SELECT COUNT(DISTINCT end_ts) FROM raid_member_activity WHERE end_ts>=? AND end_ts<=?",
                                (start, end)).fetchone()[0]


def field_cause(evidence, field, tag, old, new, start, end):
    """Why one row's field differs, or None when no rule explains it."""
    if field.startswith("war_"):
        # Without a war in the window the old overlay left the stored hourly run's numbers in place.
        return "old_row_kept_stored_run_numbers" if new == 0 and old else None
    if field.startswith("raid_"):
        if field == "raid_weekends_window":
            return "weekends_with_lines_differ:old_" + str(evidence.old_weekend_ends(start, end))
        return "raid_lines_differ" if evidence.old_raids(tag, start, end) != evidence.record_raids(tag, start, end) \
            else "raid_lines_equal_window_counting"
    if field in ("donations", "donations_received"):
        return "roster_samples_not_hourly_captures"
    if field in ("townhall", "th_delta"):
        return "town_hall_sampled_daily"
    if field in ("hero_delta", "pet_delta", "equipment_delta", "troop_delta", "spell_delta", "games_delta"):
        return "levels_sampled_daily"
    if field == "capital_delta":
        return "profile_counter_hourly_not_capture"
    if field in ("status", "flags"):
        return "follows_inputs"
    return None


def compare(work, old_path, new_path, report_path):
    old, new = (json.loads(Path(path).read_text(encoding="utf-8")) for path in (old_path, new_path))
    evidence = Evidence(work)
    record_start = evidence.record_start()
    report = {"at": old["at"], "periods": old["periods"], "sample": len(old["sample"]), "record_roster_start": record_start,
              "clans": {}, "players": {}, "unexplained": []}
    for period, (start, end) in old["periods"].items():
        counts, rosters = Counter(), Counter()
        for scope, before in old["clans"][period].items():
            after = new["clans"][period][scope]
            if before["status"] != after["status"]:
                report["unexplained"].append({"period": period, "scope": scope, "kind": "status",
                                              "old": before["status"], "new": after["status"]})
                continue
            if before["status"] != "complete":
                continue
            if before["notes"] != after["notes"]:
                counts["notes:roster_time_from_" + ("record_roster" if end >= record_start else "old_capture")] += 1
            if scope != "ALL":
                continue
            for tag in before["rows"].keys() - after["rows"].keys():
                last = evidence.old_last_capture(tag, end)
                rosters["old_only:" + ("left_before_record_roster" if last and end - last > 0 else "unexplained")] += 1
            for tag in after["rows"].keys() - before["rows"].keys():
                joined = evidence.record_membership_start(tag)
                old_time = evidence.old_roster_time(end)
                # Before the record's rosters, a copied stay runs from first to last sighting in one clan,
                # across any absence in between.
                cause = ("copied_stay_spans_an_absence" if end < record_start else
                         "joined_after_old_run" if joined and old_time and joined // SECOND > old_time else
                         "record_roster_at_end_not_old_run")
                rosters["new_only:" + cause] += 1
            for tag in before["rows"].keys() & after["rows"].keys():
                a, b = before["rows"][tag], after["rows"][tag]
                for field in (*FIELDS, "flags", "clan_code"):
                    if a.get(field) == b.get(field):
                        continue
                    cause = field_cause(evidence, field, tag, a.get(field), b.get(field), start, end)
                    counts[f"{field}:{cause or 'unexplained'}"] += 1
                    if cause is None:
                        report["unexplained"].append({"period": period, "tag": tag, "field": field,
                                                      "old": a.get(field), "new": b.get(field)})
            for name in before["sheets"]:
                if before["sheets"][name] != after["sheets"].get(name):
                    counts[f"sheet_changed:{name}"] += 1
        players = Counter()
        for tag in old["sample"]:
            a, b = old["players"][period].get(tag), new["players"][period].get(tag)
            if a == b:
                players["unchanged"] += 1
                continue
            if (a is None) != (b is None):
                players["presence:" + ("old_only" if b is None else "new_only")] += 1
                continue
            for title in a:
                if a[title] == b.get(title):
                    continue
                if title == "Clan History":
                    players["Clan History:stays_from_15_minute_record_rosters"] += 1
                    continue
                for before_row, after_row in zip(a[title], b[title]):
                    if before_row == after_row:
                        continue
                    label = before_row[0]
                    cause = {"Trend": "trend_worked_out_from_record_seasons",
                             "Clan": "old_stored_row_clan_not_last_family_clan"}.get(label, "follows_inputs")
                    players[f"{title}:{label}:{cause}"] += 1
        report["clans"][period] = {"rows_compared": len(old["clans"][period]["ALL"].get("rows", {})),
                                   "roster": dict(rosters), "fields": dict(sorted(counts.items()))}
        report["players"][period] = dict(players)
    Path(report_path).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return {"unexplained": len(report["unexplained"]), "clans": report["clans"], "players": report["players"]}


def main(argv):
    if argv[:1] == ["capture"]:
        sys.path.insert(0, str(Path(argv[3]).resolve()))
        print(json.dumps(capture(*argv[1:3])))
    elif argv[:1] == ["compare"]:
        print(json.dumps(compare(*argv[1:5]), indent=1, default=str))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
