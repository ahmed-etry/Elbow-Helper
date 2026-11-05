"""Test-only rehearsal of clan health's regular wars moving to the family record.

Three steps:

``python -m tests.data.health_move_rehearsal prepare <data folder> <work folder>``
    From the new tree's root. Copies ``family/family.sqlite3``,
    ``clan_health/clan_health.db`` and ``war/war_cache.json`` into the work
    folder and runs the startup copy of the old wars into the record copy.

``python <this file> capture <work folder> <result file> <source tree>``
    The source tree's code is used. Builds, on fresh copies
    of the work folder's files, every health view leadership can produce:
    the clan health workbook for every clan and the whole family, the player
    workbook for a fixed sample of accounts, every account's regular-war
    attack rows, and each account's war-by-war inputs, in old clan health's
    capture view (health) and the completed view (attendance), for the last
    30 days, 3 months and February to October. Clash, Discord and the LLM are
    never called.

``python -m tests.data.health_move_rehearsal compare <work folder> <old result> <new result> <report file>``
    From the new tree's root. Explains every difference from the two copies;
    unexplained ones are listed.

The data folder is never written. Results keep account tags but never names.
"""

import asyncio
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

SECOND = 1_000_000
DAY = 86400
FILES = {"family.sqlite3": "family/family.sqlite3", "clan_health.db": "clan_health/clan_health.db",
         "war_cache.json": "war/war_cache.json"}
FEBRUARY = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
# Accounts per clan whose whole player workbook is compared.
SAMPLE = 3
ROW_FIELDS = ("status", "war_events_joined", "war_hits_expected", "war_hits_used", "war_missed", "war_stars_total",
              "war_destruction_total", "war_attack_count", "regular_war_events_joined", "regular_war_hits_expected",
              "regular_war_hits_used", "regular_war_missed", "cwl_events_joined", "cwl_hits_expected",
              "cwl_hits_used", "cwl_missed", "war_attendance_verdict", "war_hit_usage_verdict",
              "cwl_participation_verdict", "cwl_hit_usage_verdict", "priority_score")
CARDS = ("war_attendance", "war_hit_usage", "cwl_participation", "cwl_hit_usage")
WAR_INPUTS = ("war_events_joined", "war_hits_expected", "war_hits_used", "war_missed", "war_stars_total",
              "war_destruction_total", "war_attack_count", "regular_war_events_joined", "regular_war_hits_expected",
              "regular_war_hits_used", "regular_war_missed")


def periods(at):
    return {"last_30_days": (at - 30 * DAY, at), "three_months": (at - 92 * DAY, at), "feb_oct": (FEBRUARY, at)}


def moment(value):
    return datetime.fromtimestamp(value, tz=timezone.utc)


def digest(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def connect(path):
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


# Prepare -----------------------------------------------------------------------------


async def prepare(data, work):
    from elbow_helper.data.store import FamilyStore
    from elbow_helper.data.store.wars_import import import_wars
    data, work = Path(data), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    for name, source in FILES.items():
        shutil.copyfile(data / source, work / name)
    store = FamilyStore(work / "family.sqlite3")
    await store.initialize()
    imported = await store.call(import_wars, work / "clan_health.db", work / "war_cache.json")
    await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    at = await store.call(lambda c: c.execute(
        "SELECT MAX(last_attempt_at) FROM war_collection_state WHERE source_kind='current_war'").fetchone()[0])
    prepared = {"at": at // SECOND, "as_of": moment(at // SECOND).isoformat(),
                "import": {"imported": imported["imported"], "wars": imported["checks"]["wars"]}}
    (work / "prepared.json").write_text(json.dumps(prepared, indent=1, default=str), encoding="utf-8")
    return prepared


# Capture -----------------------------------------------------------------------------


def masked(sheet, name_column):
    """A sheet's rows with the account name column left out."""
    return [[cell for index, cell in enumerate(row) if index != name_column] for row in sheet]


def row_view(row):
    view = {key: (round(row[key], 3) if isinstance(row.get(key), float) else row.get(key)) for key in ROW_FIELDS}
    view["flags"] = sorted(row.get("flags") or [])
    cards = row.get("signal_cards") or {}
    view["cards"] = {name: {key: (cards.get(name) or {}).get(key) for key in ("verdict", "current", "reason", "target")}
                     for name in CARDS}
    return view


class Side:
    """One tree's clan health over its own copies of the work files."""

    def __init__(self, folder, new):
        from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
        from elbow_helper.features.clan_health.database import ClanHealthRepository
        self.new = new
        self.repository = ClanHealthRepository(folder / "clan_health.db")
        self.reads = self.regular = None
        if new:
            from elbow_helper.data.reads import WarReads
            from elbow_helper.data.store import FamilyStore
            from elbow_helper.features.clan_health.record_wars import RegularWars
            self.reads = WarReads(FamilyStore(folder / "family.sqlite3"))
            self.regular = RegularWars(self.reads)
            self.league = RegularWars(self.reads, types=("CWL",))
            self.analyzer = ClanHealthAnalyzer(self.repository, self.regular, self.league)
        else:
            self.analyzer = ClanHealthAnalyzer(self.repository)

    def clan_export(self, code, start, end):
        from elbow_helper.configuration.clans import CLAN_NAMES
        from elbow_helper.features.clan_health.config import CLAN_EXPORT_ORDER
        selected = list(CLAN_EXPORT_ORDER) if code == "ALL" else [code]
        run, stored = self.repository.latest_report_before(cycle_end_ts=end, selected_clans=selected)
        grouped = {clan: {"clan_code": clan, "clan_name": CLAN_NAMES[clan], "players": []} for clan in selected}
        for row in stored:
            row["flags"] = json.loads(row.get("flags_json") or "[]")
            grouped[row["clan_code"]]["players"].append(row)
        sheets, rows, totals = self.analyzer.build_sheets(
            selected_clans=selected, clan_entries=[grouped[clan] for clan in selected],
            cycle_start=moment(start), cycle_end=moment(end))
        tags = {id(row): row["player_tag"] for row in rows}
        return {"run": run["run_id"] if run else None, "overall": totals,
                "rows": {tags[id(row)]: row_view(row) for row in rows},
                "sheets": {name: masked(sheet, 1) for name, sheet in sheets}}

    def player_export(self, tag, name, start, end, at):
        from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin

        class Harness(ClanHealthPlayerCommandMixin):
            pass

        harness = Harness()
        harness.repository, harness.analyzer = self.repository, self.analyzer
        harness.clash_client, harness.collector = SimpleNamespace(configured=False), None
        if self.new:
            harness.regular_wars = self.regular
            harness.league_wars = self.league
        custom = name != "last_30_days"
        export = asyncio.run(harness.build_player_health_export(
            player_tag=tag, now=moment(at), window_mode="custom" if custom else "last_30d",
            season_key=f"rehearsal {name}", trend_season_key="2026-10", cycle_start=moment(start),
            cycle_end=moment(end), window_label=name, date_from=moment(start).date().isoformat() if custom else None,
            date_to=moment(end).date().isoformat() if custom else None))
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

    def attacks(self, start, end):
        """Every account's regular-war attack rows, as the player workbook lists them."""
        if not self.new:
            with closing(connect(self.repository.path)) as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM war_attacks WHERE war_type='REG' AND end_ts>=? AND end_ts<=? "
                    "ORDER BY end_ts DESC, war_id DESC, attack_order", (start, end))]
        else:
            from elbow_helper.features.clan_health.record_wars import attack_row
            rows = [attack_row(row, self.regular.codes) for row in self.pages(
                self.capture_attacks, self.regular._clans(), start * SECOND, end * SECOND + SECOND)]
        by_tag = defaultdict(list)
        for row in rows:
            by_tag[row["player_tag"]].append({key: row[key] for key in (
                "war_id", "clan_code", "end_ts", "attack_order", "defender_tag", "defender_map_position",
                "defender_townhall", "stars", "destruction", "fresh_attack")})
        return dict(by_tag)

    async def capture_attacks(self, *args, **kwargs):
        from elbow_helper.features.clan_health.record_wars import capture_page
        return await capture_page(self.reads, "attacks", *args, **kwargs)

    async def capture_participation(self, *args, **kwargs):
        from elbow_helper.features.clan_health.record_wars import capture_page
        return await capture_page(self.reads, "participation", *args, **kwargs)

    def pages(self, method, *args, **kwargs):
        rows, offset = [], 0
        while offset is not None:
            page = asyncio.run(method(*args, **kwargs, limit=200, offset=offset))
            rows.extend(page["rows"])
            offset = page["next_offset"]
        return rows

    def contributions(self, start, end):
        """Each account's regular wars as health counts them, war by war."""
        by_tag = defaultdict(list)
        if not self.new:
            with closing(connect(self.repository.path)) as connection:
                for row in connection.execute(
                        "SELECT war_id,clan_code,player_tag,end_ts,attacks_expected,attacks_used,stars,attack_count "
                        "FROM war_activity WHERE war_type='REG' AND end_ts>=? AND end_ts<=?", (start, end)):
                    by_tag[row["player_tag"]].append({
                        "war": row["war_id"], "clan_code": row["clan_code"], "time": row["end_ts"],
                        "expected": row["attacks_expected"], "used": row["attacks_used"], "stars": row["stars"],
                        "attacks": row["attack_count"]})
        else:
            for row in self.pages(self.capture_participation, self.regular._clans(), start * SECOND,
                                  end * SECOND + SECOND):
                by_tag[row["player_tag"]].append({
                    "war": row["war_id"], "clan_code": self.regular.codes[row["clan_tag"]],
                    "time": row["time"] // SECOND, "state": row["state"], "expected": row["attacks_expected"],
                    "used": row["attacks_used"], "stars": row["stars"], "attacks": row["attacks_measured"]})
        return dict(by_tag)

    def completed(self, start, end):
        """Each account's ended regular wars in [start, end), as attendance reads them."""
        from elbow_helper.configuration.clans import CLANS
        by_tag = defaultdict(list)
        if not self.new:
            snapshot = self.repository.attendance(clan_codes=list(CLANS), war_type="REG", after_ts=start, before_ts=end)
            for row in snapshot["rows"]:
                by_tag[row["player_tag"]].append({
                    "war": row["war_id"], "aliases": row["war_aliases"], "clan_code": row["clan_code"],
                    "time": row["end_ts"], "expected": row["attacks_expected"], "used": row["attacks_used"],
                    "missed": row["missed"], "stars": row["observed_attack_performance"]["stars"]})
        else:
            for row in self.pages(self.reads.participation, self.regular._clans(), start * SECOND, end * SECOND):
                by_tag[row["player_tag"]].append({
                    "war": row["war_id"], "clan_code": self.regular.codes[row["clan_tag"]],
                    "time": row["end_at"] // SECOND, "expected": row["attacks_expected"],
                    "used": row["attacks_used"], "missed": row["missed"], "stars": row["stars"]})
        return dict(by_tag)


def capture(work, out):
    from elbow_helper.configuration.clans import CLANS
    work = Path(work)
    new = importlib.util.find_spec("elbow_helper.features.clan_health.record_wars") is not None
    prepared = json.loads((work / "prepared.json").read_text(encoding="utf-8"))
    side_folder = work / ("new" if new else "old")
    side_folder.mkdir(exist_ok=True)
    for name in ("family.sqlite3", "clan_health.db"):
        shutil.copyfile(work / name, side_folder / name)
    side = Side(side_folder, new)
    at = prepared["at"]
    result = {"side": "new" if new else "old", "as_of": prepared["as_of"], "periods": periods(at),
              "clan_exports": {}, "player_exports": {}, "attacks": {}, "contributions": {}, "completed": {}}
    run, roster = side.repository.latest_report_before(cycle_end_ts=at, selected_clans=list(CLANS))
    sample = sorted(tag for code in CLANS for tag in sorted(
        row["player_tag"] for row in roster if row["clan_code"] == code)[:SAMPLE])
    result["sample"] = sample
    for name, (start, end) in periods(at).items():
        print(result["side"], name, flush=True)
        result["clan_exports"][name] = {code: side.clan_export(code, start, end) for code in ("ALL", *CLANS)}
        result["player_exports"][name] = {tag: side.player_export(tag, name, start, end, at) for tag in sample}
        result["attacks"][name] = side.attacks(start, end)
        result["contributions"][name] = side.contributions(start, end)
        result["completed"][name] = side.completed(start, end)
    Path(out).write_text(json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return {"side": result["side"], "written": str(out)}


def main(argv):
    if argv[:1] == ["prepare"]:
        print(json.dumps(asyncio.run(prepare(*argv[1:3])), indent=1, default=str))
    elif argv[:1] == ["capture"]:
        sys.path.insert(0, str(Path(argv[3]).resolve()))
        print(json.dumps(capture(*argv[1:3])))
    elif argv[:1] == ["compare"]:
        print(json.dumps(compare(*argv[1:5]), indent=1, default=str))
    else:
        raise SystemExit(__doc__)


# Compare -----------------------------------------------------------------------------


class Evidence:
    """What the record copy and the old clan health copy hold about a war."""

    def __init__(self, work):
        self.record = connect(Path(work) / "family.sqlite3")
        self.old = connect(Path(work) / "clan_health.db")
        self.alias = {}
        self.aliases = defaultdict(set)
        # Whether old clan health saw a war end, per clan side, and what each old copy captured.
        self.seen_ended = defaultdict(bool)
        self.captured = {}
        for row in self.record.execute(
                "SELECT war_id,legacy_id,clan_code,capture_state FROM war_legacy_ids WHERE source='clan_health'"):
            self.alias[row["clan_code"], row["legacy_id"]] = row["war_id"]
            self.aliases[row["war_id"]].add((row["clan_code"], row["legacy_id"]))
            self.seen_ended[row["war_id"], row["clan_code"]] |= row["capture_state"] == "warEnded"
            self.captured[row["clan_code"], row["legacy_id"]] = row["capture_state"]
        self.wars = {row["id"]: dict(row) for row in self.record.execute(
            "SELECT id,type,type_basis,state,end_at,last_seen_at,final_at FROM wars")}
        self.lineups = defaultdict(set)
        for row in self.record.execute("SELECT war_id,player_tag FROM war_members"):
            self.lineups[row["war_id"]].add(row["player_tag"])
        self.old_rows = defaultdict(list)
        for row in self.old.execute("SELECT war_id,clan_code,player_tag,end_ts,source FROM war_activity WHERE war_type='REG'"):
            self.old_rows[row["clan_code"], row["war_id"]].append(dict(row))
        self.old_wars = {(row["clan_code"], row["war_id"]): dict(row) for row in self.old.execute(
            "SELECT war_id,clan_code,state,end_ts,last_seen_ts FROM wars WHERE war_type='REG'")}

    def war_of(self, code, old_id):
        return self.alias.get((code, old_id))

    def old_time(self, war, tag):
        """When old clan health last captured this account in this record war, by any of its old IDs."""
        times = [row["end_ts"] for code, legacy in self.aliases.get(war, ())
                 for row in self.old_rows.get((code, legacy), ()) if row["player_tag"] == tag]
        return max(times) if times else None

    def not_copied(self, code, old_id):
        """Why an old war has no record war: what old clan health itself knew about it."""
        war = self.old_wars.get((code, old_id))
        states = sorted({row["source"].split(":")[-1] for row in self.old_rows.get((code, old_id), ())})
        if war is None and states and all(state in ("preparation", "inWar") for state in states):
            return "old_war_never_seen_ended"
        if war is not None and war["state"] != "warEnded":
            return "old_war_never_seen_ended"
        return None


def war_differences(evidence, tag, old_rows, new_rows, start, end, *, completed=False):
    """War-by-war reasons one account's regular wars differ; None marks an unexplained one."""
    reasons = []
    matched = defaultdict(list)
    for row in old_rows:
        ids = row.get("aliases") or [row["war"]]
        war = next((evidence.war_of(row["clan_code"], old_id) for old_id in ids
                    if evidence.war_of(row["clan_code"], old_id) is not None), None)
        if war is None:
            cause = evidence.not_copied(row["clan_code"], row["war"])
            reasons.append(("old_only", row["war"], cause))
        else:
            matched[war].append(row)
    new_by_war = {row["war"]: row for row in new_rows}
    for war in sorted(set(matched) | set(new_by_war)):
        olds, current = matched.get(war, []), new_by_war.get(war)
        info = evidence.wars[war]
        if current is None:
            inside = (start <= info["end_at"] // SECOND < end) if completed else start <= (
                info["end_at"] if info["state"] == "warEnded" else info["last_seen_at"]) // SECOND <= end
            if tag not in evidence.lineups[war]:
                reasons.append(("old_only", war, "record_lineup_lacks_account"))
            elif not inside:
                reasons.append(("old_only", war, "record_time_outside_period"))
            else:
                reasons.append(("old_only", war, None))
            continue
        if not olds:
            old_time = evidence.old_time(war, tag)
            if not evidence.aliases.get(war):
                reasons.append(("new_only", war, "war_only_in_record"))
            elif completed and not evidence.seen_ended[war, current["clan_code"]] and info["state"] == "warEnded":
                # Old attendance counts only wars it saw end; the record closed this one from the war log.
                reasons.append(("new_only", war, "old_saw_war_only_before_its_end"))
            elif old_time is None:
                reasons.append(("new_only", war, "old_lineup_lacks_account"))
            elif not (start <= old_time < end if completed else start <= old_time <= end):
                reasons.append(("new_only", war, "old_time_outside_period"))
            else:
                reasons.append(("new_only", war, None))
            continue
        if len(olds) > 1:
            reasons.append(("count", war, "old_saved_war_twice"))
        old = olds[0]
        for field in ("expected", "used", "missed", "stars", "attacks"):
            if field not in current or old.get(field) == current.get(field):
                continue
            if field in ("stars", "attacks") and current.get("attacks") == 0 and (current.get("used") or 0) > 0:
                cause = "record_has_no_attack_detail"
            elif field == "missed" and (old.get("missed") is None) != (current.get("missed") is None):
                cause = "miss_known_on_one_side"
            elif current.get("state", "warEnded") != "warEnded":
                cause = "record_checked_ongoing_war_later"
            elif not completed and old.get("time") != current.get("time"):
                cause = "old_capture_before_war_end"
            else:
                cause = None
            reasons.append((field, war, cause))
    return reasons


def totals(rows, *, completed=False):
    """Account totals as health (or attendance) adds the war rows up."""
    known = [row for row in rows if row.get("expected") is not None and row.get("used") is not None]
    return {"wars": len({row["war"] for row in rows}), "expected": sum(row["expected"] for row in known),
            "used": sum(row["used"] for row in known),
            **({"missed": sum(row["missed"] for row in rows if row.get("missed") is not None)} if completed else {}),
            "stars": sum(row.get("stars") or 0 for row in rows)}


def clan_wars(contributions):
    """Wars each clan fought in the period with a stored lineup, as health counts them."""
    wars = defaultdict(set)
    for rows in contributions.values():
        for row in rows:
            wars[row["clan_code"]].add(row["war"])
    return {code: len(values) for code, values in wars.items()}


def compare(work, old_path, new_path, report_path):
    old, new = (json.loads(Path(path).read_text(encoding="utf-8")) for path in (old_path, new_path))
    evidence = Evidence(work)
    links = {row["player_tag"]: digest(row["discord_id"]) for row in evidence.record.execute(
        "SELECT a.player_tag,m.discord_id FROM account_links l JOIN accounts a ON a.id=l.account_id "
        "JOIN members m ON m.id=l.member_id WHERE l.to_at IS NULL")}
    summary = {"periods": old["periods"], "as_of": old["as_of"], "sample": old["sample"]}
    reasons_count, unexplained, examples = Counter(), [], defaultdict(list)
    sections = {}

    def note(section, reasons, key):
        for kind, war, cause in reasons:
            label = f"{kind}:{cause}" if cause else f"{kind}:unexplained"
            reasons_count[section, label] += 1
            if cause is None:
                unexplained.append({"section": section, "key": key, "war": war, "kind": kind})
            elif len(examples[section, label]) < 3:
                examples[section, label].append({"key": key, "war": war})

    for period, (start, end) in old["periods"].items():
        olds, news = old["contributions"][period], new["contributions"][period]
        explained_accounts, account_reasons = set(), {}
        for tag in sorted(set(olds) | set(news)):
            o, n = olds.get(tag, []), news.get(tag, [])
            if totals(o) == totals(n):
                continue
            reasons = war_differences(evidence, tag, o, n, start, end)
            account_reasons[tag] = reasons
            note(f"health inputs {period}", reasons, tag)
            if reasons and all(cause is not None for _, _, cause in reasons):
                explained_accounts.add(tag)
            elif not reasons:
                unexplained.append({"section": f"health inputs {period}", "key": tag, "kind": "totals_without_war_difference"})
        old_counts, new_counts = clan_wars(olds), clan_wars(news)
        clan_count_changes = {code: (old_counts.get(code, 0), new_counts.get(code, 0))
                              for code in sorted(set(old_counts) | set(new_counts))
                              if old_counts.get(code, 0) != new_counts.get(code, 0)}
        rows_compared = rows_changed = 0
        member_changes = defaultdict(lambda: [Counter(), Counter(), set()])
        for scope, old_export in old["clan_exports"][period].items():
            new_export = new["clan_exports"][period][scope]
            if old_export["run"] != new_export["run"]:
                unexplained.append({"section": f"clan workbook {period}", "key": scope, "kind": "different_roster"})
            if old_export["sheets"]["Activity"] != new_export["sheets"]["Activity"]:
                unexplained.append({"section": f"clan workbook {period}", "key": scope, "kind": "activity_sheet"})
            for tag, row in old_export["rows"].items():
                other = new_export["rows"].get(tag)
                rows_compared += 1
                if other is None:
                    unexplained.append({"section": f"clan workbook {period}", "key": f"{scope} {tag}", "kind": "row_missing"})
                    continue
                changed = [key for key in row if row[key] != other[key]]
                if scope == "ALL":
                    member = links.get(tag)
                    if member:
                        member_changes[member][0].update({key: row[key] for key in WAR_INPUTS if isinstance(row[key], (int, float))})
                        member_changes[member][1].update({key: other[key] for key in WAR_INPUTS if isinstance(other[key], (int, float))})
                        member_changes[member][2].add(tag)
                if not changed:
                    continue
                rows_changed += scope == "ALL"
                inputs_changed = any(key in WAR_INPUTS for key in changed)
                cwl_changed = [key for key in changed if key.startswith("cwl_")]
                if cwl_changed:
                    unexplained.append({"section": f"clan workbook {period}", "key": f"{scope} {tag}", "kind": "league_wars_changed"})
                elif inputs_changed and tag not in explained_accounts:
                    unexplained.append({"section": f"clan workbook {period}", "key": f"{scope} {tag}", "kind": "inputs_not_explained"})
                elif not inputs_changed and row["regular_war_events_joined"] and not any(
                        code in clan_count_changes for code in {scope} | ({row.get("clan_code")} if row.get("clan_code") else set())):
                    # Scores follow inputs or the clan's war count; both unchanged here.
                    if not clan_count_changes:
                        unexplained.append({"section": f"clan workbook {period}", "key": f"{scope} {tag}",
                                            "kind": "score_without_input_change", "fields": changed})
        member_diffs = sum(1 for before, after, _ in member_changes.values() if before != after)
        member_unexplained = sum(1 for before, after, tags in member_changes.values()
                                 if before != after and not tags <= explained_accounts | {
                                     tag for tag in tags if totals(olds.get(tag, [])) == totals(news.get(tag, []))})
        if member_unexplained:
            unexplained.append({"section": f"members {period}", "key": "member totals", "kind": "member_not_explained",
                                "count": member_unexplained})
        # Attack rows
        attack_changes = Counter()
        for tag in sorted(set(old["attacks"][period]) | set(new["attacks"][period])):
            o, n = old["attacks"][period].get(tag, []), new["attacks"][period].get(tag, [])
            keyed_new = {(row["war_id"], row["attack_order"]): row for row in n}
            seen = set()
            for row in o:
                war = evidence.war_of(row["clan_code"], row["war_id"])
                match = keyed_new.get((war, row["attack_order"]))
                if match is None:
                    if war is None:
                        cause = evidence.not_copied(row["clan_code"], row["war_id"])
                    else:
                        info = evidence.wars[war]
                        record_time = (info["end_at"] if info["state"] == "warEnded" else info["last_seen_at"]) // SECOND
                        cause = "record_time_outside_period" if not start <= record_time <= end else None
                    attack_changes["old_only:" + (cause or "unexplained")] += 1
                    if cause is None:
                        unexplained.append({"section": f"attack rows {period}", "key": tag, "war": row["war_id"],
                                            "kind": "old_only"})
                    continue
                seen.add((war, row["attack_order"]))
                for field in ("end_ts", "clan_code", "defender_tag", "defender_map_position", "defender_townhall",
                              "stars", "destruction", "fresh_attack"):
                    a, b = row[field], match[field]
                    if field == "fresh_attack":
                        a = bool(a) if a is not None else None
                    if a == b or field == "destruction" and abs(a - b) < 1e-6:
                        continue
                    if field == "end_ts":
                        info = evidence.wars[war]
                        if info["state"] != "warEnded":
                            cause = "record_checked_ongoing_war_later"
                        elif not evidence.seen_ended[war, row["clan_code"]]:
                            cause = "old_saw_war_only_before_its_end"
                        elif evidence.captured.get((row["clan_code"], row["war_id"])) != "warEnded":
                            # The row comes from an earlier copy of a war old clan health saved twice.
                            cause = "old_copy_captured_before_war_end"
                        elif 0 < b - a <= 600:
                            cause = "war_log_end_later_than_current_war_end"
                        else:
                            cause = None
                    elif field == "fresh_attack" and b is None:
                        cause = "first_hit_unknown_in_record"
                    elif field == "defender_map_position" and b == 0:
                        cause = "position_unknown_in_record"
                    else:
                        cause = None
                    attack_changes[f"{field}:" + (cause or "unexplained")] += 1
                    if cause is None:
                        unexplained.append({"section": f"attack rows {period}", "key": tag, "war": war,
                                            "kind": field, "old": a, "new": b})
            for key, row in keyed_new.items():
                if key in seen:
                    continue
                war = key[0]
                old_time = evidence.old_time(war, tag)
                cause = ("war_only_in_record" if not evidence.aliases.get(war) else
                         "old_time_outside_period" if old_time is not None and not start <= old_time <= end else
                         "old_has_no_attack_detail")
                attack_changes["new_only:" + cause] += 1
        # Completed view
        completed_changes, completed_accounts = Counter(), 0
        for tag in sorted(set(old["completed"][period]) | set(new["completed"][period])):
            o, n = old["completed"][period].get(tag, []), new["completed"][period].get(tag, [])
            if totals(o, completed=True) == totals(n, completed=True):
                continue
            completed_accounts += 1
            reasons = war_differences(evidence, tag, o, n, start, end, completed=True)
            note(f"attendance {period}", reasons, tag)
            for kind, _, cause in reasons:
                completed_changes[f"{kind}:{cause or 'unexplained'}"] += 1
            if not reasons:
                unexplained.append({"section": f"attendance {period}", "key": tag, "kind": "totals_without_war_difference"})
        # Player workbooks
        player_changes = Counter()
        for tag in old["sample"]:
            a, b = old["player_exports"][period].get(tag), new["player_exports"][period].get(tag)
            if a == b:
                continue
            if (a is None) != (b is None):
                unexplained.append({"section": f"player workbook {period}", "key": tag, "kind": "presence"})
                continue
            for title in a:
                if a[title] == b[title]:
                    continue
                player_changes[title] += 1
                explained = (title in ("Overview", "Breakdown") and (tag in explained_accounts or bool(clan_count_changes))
                             or title == "Wars")
                if not explained:
                    unexplained.append({"section": f"player workbook {period}", "key": tag, "kind": title})
        sections[period] = {
            "accounts_with_changed_inputs": len(account_reasons),
            "accounts_explained": len(explained_accounts),
            "clan_war_counts_changed": clan_count_changes,
            "workbook_rows_compared": rows_compared, "family_rows_changed": rows_changed,
            "members_changed": member_diffs, "attack_rows": dict(attack_changes),
            "attendance_accounts_changed": completed_accounts, "attendance_reasons": dict(completed_changes),
            "player_workbook_sheets_changed": dict(player_changes)}
    result = {"summary": summary, "sections": sections,
              "reasons": {f"{section} | {label}": count for (section, label), count in sorted(reasons_count.items())},
              "examples": {f"{section} | {label}": rows for (section, label), rows in sorted(examples.items())},
              "unexplained": unexplained}
    Path(report_path).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return {"unexplained": len(unexplained), "sections": sections}


if __name__ == "__main__":
    main(sys.argv[1:])
