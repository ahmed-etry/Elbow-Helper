"""Read-only, checked snapshots of both old profile sources; retain only changes."""

from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from dataclasses import dataclass
from hashlib import sha256
import json
from itertools import groupby
from pathlib import Path
import sqlite3

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import now_us
from .legacy_links import ImportMismatch
from .player_projection import COUNTERS, HOUR_US, LEGEND_FAST, LEGEND_SLOW
from .projections import canonical_json, nonnegative_int
from .ranked_import import TABLES, timestamp


HEALTH = "legacy_health"
RANKED = "legacy_ranked"
TOTALS = ("hero_sum", "pet_sum", "equipment_sum", "troop_sum", "spell_sum", "games_total")
HEALTH_FIELDS = {"townhall": "townHallLevel", "trophies": "trophies", "donations": "donations",
                 "donations_received": "donationsReceived", "war_stars": "warStars",
                 "capital_contrib": "clanCapitalContributions", "attack_wins": "attackWins"}
HEALTH_COLUMNS = ("captured_ts", "clan_code", "player_tag", "player_name", *HEALTH_FIELDS, *TOTALS)
RANKED_COUNTERS = {"trophies", "bestTrophies", "attackWins", "defenseWins"}


@dataclass(frozen=True)
class ProfileSnapshot:
    version: int
    digest: str
    frozen_at: int
    events: dict
    coverage: dict
    counts: dict
    outcomes: dict
    suppressed: dict
    cutoffs: dict


def record_cutoffs(connection):
    """The record wins starting at its first actual profile fact, not an ownership date."""
    return {row[0]: row[1] for row in connection.execute(
        "SELECT a.player_tag,MIN(p.at) FROM accounts a LEFT JOIN ("
        "SELECT account_id,sampled_at AS at FROM player_counters UNION ALL "
        "SELECT account_id,observed_at FROM player_observations UNION ALL "
        "SELECT account_id,observed_at FROM player_items) p ON p.account_id=a.id GROUP BY a.id")}


def watch(facts, tag, origin, at, status, fields):
    fact = facts.setdefault((tag, origin), {
        "first_attempt_at": at, "last_attempt_at": at, "first_good_at": None, "last_good_at": None,
        "samples": 0, "partial_samples": 0, "failed_samples": 0, "gaps": [], "failures": [], "fields": Counter()})
    if fact["samples"] and at <= fact["last_attempt_at"]:
        raise ImportMismatch("profile_sample_order")
    if at > fact["last_attempt_at"] + 2 * HOUR_US:
        fact["gaps"].append({"after": fact["last_attempt_at"], "before": at, "reason": "missed_capture"})
    fact["last_attempt_at"] = at
    fact["samples"] += 1
    fact["fields"].update(fields)
    if status == "ok":
        if fact["first_good_at"] is None:
            fact["first_good_at"] = at
        for failure in reversed(fact["failures"]):
            if failure["next_good_at"] is not None:
                break
            failure["next_good_at"] = at
        fact["last_good_at"] = at
    else:
        fact["partial_samples" if status == "deep_unavailable" else "failed_samples"] += 1
        if (fact["failures"] and fact["failures"][-1]["next_good_at"] is None
                and fact["failures"][-1]["reason"] == status):
            fact["failures"][-1]["last_at"] = at
            fact["failures"][-1]["samples"] += 1
        else:
            fact["failures"].append({"first_at": at, "last_at": at, "samples": 1, "reason": status,
                                     "previous_good_at": fact["last_good_at"], "next_good_at": None})


def ranked_fields(raw, status):
    values = json.loads(raw)
    if not isinstance(values, dict) or (status != "ok" and values):
        raise ImportMismatch("bad_profile_ranked_fields")
    if values.keys() - (RANKED_COUNTERS | {"leagueTier", "league", "legendStatistics"}):
        raise ImportMismatch("unknown_profile_ranked_field")
    counters, fields = {}, {}
    for key, value in values.items():
        if key in RANKED_COUNTERS:
            if not nonnegative_int(value):
                raise ImportMismatch("bad_profile_counter")
            counters[key] = value
        else:
            if not isinstance(value, dict):
                raise ImportMismatch("bad_profile_object")
            canonical_json(value)
            if key == "legendStatistics":
                if value.keys() - (LEGEND_FAST | LEGEND_SLOW):
                    raise ImportMismatch("unknown_profile_legend_field")
                counters[key] = {k: v for k, v in value.items() if k in LEGEND_FAST}
                fields.update({f"legendStatistics.{k}": v for k, v in value.items() if k in LEGEND_SLOW})
            else:
                fields[key] = value
    return counters, fields, {}


def snapshot_profiles(health_path, ranked_path, cutoffs, at=None):
    """Pin both read transactions before scanning; no source adoption or source writes."""
    frozen_at = now_us() if at is None else at
    if not nonnegative_int(frozen_at):
        raise ValueError("invalid_freeze_time")
    events, coverage = defaultdict(dict), {}
    counts, outcomes, suppressed = Counter(), defaultdict(Counter), Counter()
    hasher, spans, previous, resumed = sha256(), {}, {}, set()

    def consume(tag, origin, stamp, status, sections):
        key = origin + (".player_snapshots" if origin == HEALTH else ".snapshots")
        cutoff = cutoffs.get(tag)
        if cutoff is not None and stamp >= cutoff:
            outcomes[key]["skipped:record_history"] += 1
            return
        supplied = [name for section in sections for name in section]
        watch(coverage, tag, origin, stamp, status, supplied)
        if status not in {"ok", "deep_unavailable"}:
            outcomes[key]["skipped:" + status] += 1
            return
        old = previous.setdefault((tag, origin), {})
        if origin == HEALTH and tag in spans and stamp > spans[tag][1] and tag not in resumed:
            # Resume against B's final values, even if A took no sample inside B's span.
            for name in ("trophies", "attackWins"):
                old.pop(name, None)
                if name in previous.get((tag, RANKED), {}):
                    old[name] = previous[tag, RANKED][name]
            resumed.add(tag)
        if origin == HEALTH and tag in spans and spans[tag][0] <= stamp <= spans[tag][1]:
            counters = sections[0]
            for name in ("trophies", "attackWins"):
                if name in counters:
                    del counters[name]
                    suppressed[name] += 1
        retained = []
        for index, section in enumerate(sections):
            changed = {key: value for key, value in section.items() if key not in old or old[key] != value}
            # Counter and total rows contain only fields observed by THIS sample.
            retained.append(section if index in (0, 2) and changed else changed)
            old.update(section)
        if any(retained):
            event = events[tag].setdefault(stamp, [])
            event.append((origin, *retained))
            outcomes[key]["copied"] += 1
        else:
            outcomes[key]["skipped:unchanged" if supplied else "skipped:no_values"] += 1

    try:
        with ExitStack() as stack:
            sources = []
            for path, origin, versions, table, columns in (
                    (ranked_path, RANKED, {1}, "snapshots", TABLES["snapshots"]),
                    (health_path, HEALTH, {0, 1, 2}, "player_snapshots", HEALTH_COLUMNS)):
                source = stack.enter_context(closing(sqlite3.connect(
                    Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)))
                source.row_factory = sqlite3.Row
                source.execute("PRAGMA query_only=ON")
                source.execute("PRAGMA temp_store=MEMORY")
                stack.enter_context(sqlite_transaction(source))
                version = source.execute("PRAGMA user_version").fetchone()[0]
                if version not in versions:
                    raise ImportMismatch("unsupported_profile_source_version:" + origin)
                found = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
                if found != set(columns):
                    raise ImportMismatch("legacy_schema_mismatch:" + table)
                hasher.update(canonical_json([origin, version]).encode())
                sources.append((source, origin, table, columns))
            for source, origin, table, columns in sources:
                key = origin + "." + table
                counts[key] = 0
                order = "player_tag,observed_at" if origin == RANKED else "player_tag,captured_ts,clan_code"
                cursor = source.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}")
                groups = ((None, (row,)) for row in cursor) if origin == RANKED else groupby(
                    cursor, key=lambda row: (row["player_tag"], row["captured_ts"]))
                for _, capture in groups:
                    capture = list(capture)
                    row = capture[0]
                    for original in capture:
                        counts[key] += 1
                        hasher.update((canonical_json(list(original)) + "\n").encode())
                    tag = row["player_tag"]
                    if tag is None or normalize_player_tag(tag) != tag:
                        raise ImportMismatch("bad_profile_tag")
                    if origin == RANKED:
                        stamp, status = timestamp(row["observed_at"]), row["status"]
                        if status not in {"ok", "unavailable", "not_found"}:
                            raise ImportMismatch("bad_profile_status")
                        sections = ranked_fields(row["fields_json"], status)
                        if status == "ok":
                            spans[tag] = (spans.get(tag, (stamp, stamp))[0], stamp)
                    else:
                        seconds = row["captured_ts"]
                        if not nonnegative_int(seconds):
                            raise ImportMismatch("bad_profile_time")
                        stamp = seconds * 1_000_000
                        if any(original[name] is not None and not nonnegative_int(original[name])
                               for original in capture for name in (*HEALTH_FIELDS, *TOTALS)):
                            raise ImportMismatch("bad_profile_counter")
                        values, conflicts = {}, False
                        for name in (*HEALTH_FIELDS, *TOTALS):
                            alternatives = sorted({original[name] for original in capture if original[name] is not None})
                            if len(alternatives) > 1 and HEALTH_FIELDS.get(name) not in COUNTERS:
                                raise ImportMismatch("conflicting_profile_duplicate")
                            values[name] = alternatives if len(alternatives) > 1 else alternatives[0] if alternatives else None
                            conflicts |= len(alternatives) > 1
                        if len(capture) > 1:
                            reason = "conflict_evidence_merged" if conflicts else "duplicate_capture"
                            outcomes[key]["skipped:" + reason] += len(capture) - 1
                        fields = {dest: values[name] for name, dest in HEALTH_FIELDS.items() if values[name] is not None}
                        sections = ({k: v for k, v in fields.items() if k in COUNTERS},
                                    {k: v for k, v in fields.items() if k not in COUNTERS},
                                    {name: values[name] for name in TOTALS if values[name] is not None})
                        status = "deep_unavailable" if all(values[name] is None for name in ("townhall", *TOTALS)) else "ok"
                    if stamp > frozen_at:
                        raise ImportMismatch("future_profile_sample")
                    consume(tag, origin, stamp, status, sections)
    except sqlite3.Error as error:
        raise ImportMismatch("profile_source_unreadable") from error
    except (ValueError, TypeError, OverflowError) as error:
        raise ImportMismatch("bad_profile_row") from error
    return ProfileSnapshot(1, hasher.hexdigest(), frozen_at, dict(events), coverage, dict(counts),
                           {k: dict(v) for k, v in outcomes.items()}, dict(suppressed), cutoffs)
