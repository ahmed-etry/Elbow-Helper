"""Report collection incidents once, retaining health across independent deadlines."""

from dataclasses import dataclass


FAILURE_POLL_THRESHOLD = 3
OUTAGE_MIN_ACCOUNTS = 3


@dataclass
class Failure:
    consecutive: int = 0
    status: int | None = None
    warned: bool = False
    missing: bool = False


class CollectionHealth:
    def __init__(self, logger):
        self.logger = logger
        self.failures = {}
        self.latest = {}
        self.pending = {}
        self.outages = set()
        self.missing_players = set()
        self.reported_missing_players = set()

    def restore_missing(self, states):
        self.reported_missing_players.update(states)
        for tag, missing in states.items():
            if missing:
                self.missing_players.add(tag)
                self.failures[tag, "player snapshot"] = Failure(missing=True)

    def missing_state(self):
        return {tag: tag in self.missing_players for tag in self.reported_missing_players}

    def record(self, tag, kind, status, *, ok=False):
        # Success requires a validated result, not merely a successful HTTP response.
        self.pending[tag, kind] = (ok, status)

    def retain(self, tags):
        self.failures = {key: value for key, value in self.failures.items() if key[0] in tags}
        self.latest = {key: value for key, value in self.latest.items() if key[0] in tags}
        self.pending = {key: value for key, value in self.pending.items() if key[0] in tags}
        self.missing_players.intersection_update(tags)
        self.reported_missing_players.intersection_update(tags)
        self.outages.intersection_update(kind for _, kind in self.latest)

    def report(self):
        pending, self.pending = self.pending, {}
        recovered = []
        for key, (ok, status) in pending.items():
            self.latest[key] = (ok, status)
            tag, kind = key
            if ok:
                failure = self.failures.pop(key, None)
                if failure is not None and failure.warned:
                    recovered.append(key)
                continue
            failure = self.failures.setdefault(key, Failure())
            failure.consecutive += 1
            failure.status = status
            if status == 404 and kind == "player snapshot":
                failure.warned = False
                failure.missing = True
            detail = "no response" if status is None else f"status {status}"
            self.logger.debug("Ranked %s unavailable for %s (%s)", kind, tag, detail)

        for kind, tags in _by_kind(recovered).items():
            if len(tags) == 1:
                self.logger.info("Ranked %s recovered for %s", kind, tags[0])
            else:
                self.logger.info("Ranked %s recovered for %s accounts: %s",
                                 kind, len(tags), _sample(tags))
        missing = {tag for (tag, _), failure in self.failures.items() if failure.missing}
        for tag in sorted(missing - self.reported_missing_players):
            self.logger.warning("Ranked player profile not found for %s (status 404)", tag)
            self.reported_missing_players.add(tag)
        for tag in sorted(self.missing_players - missing):
            self.logger.info("Ranked player %s recovered after status 404", tag)
        self.missing_players = missing

        for kind in sorted({kind for _, kind in self.latest}):
            statuses = [result for (tag, endpoint), result in self.latest.items()
                        if endpoint == kind and tag not in missing and result[1] != 404]
            # A missing player is an account problem, not evidence of an outage.
            failed = sum(not ok and status != 404 for ok, status in statuses)
            outage = len(statuses) >= OUTAGE_MIN_ACCOUNTS and failed * 2 > len(statuses)
            if outage and kind not in self.outages:
                self.logger.warning("Ranked %s outage: %s of %s accounts unavailable",
                                    kind, failed, len(statuses))
                self.outages.add(kind)
            elif not outage and kind in self.outages:
                self.logger.info("Ranked %s outage recovered: %s of %s accounts unavailable",
                                 kind, failed, len(statuses))
                self.outages.remove(kind)

        # A partial outage can cross the threshold for many accounts at once; one line per kind.
        newly_failed = {}
        for (tag, kind), failure in self.failures.items():
            if tag in missing or failure.warned or kind in self.outages:
                continue
            transient = failure.status is None or failure.status == 429 or failure.status >= 500
            if transient and failure.consecutive < FAILURE_POLL_THRESHOLD:
                continue
            newly_failed.setdefault(kind, []).append((tag, failure))
            failure.warned = True
        for kind, items in newly_failed.items():
            if len(items) == 1:
                tag, failure = items[0]
                self.logger.warning("Ranked %s unavailable for %s after %s consecutive polls (%s)",
                                    kind, tag, failure.consecutive, _detail(failure.status))
                continue
            details = sorted({_detail(failure.status) for _, failure in items})
            self.logger.warning(
                "Ranked %s unavailable for %s accounts after %s+ consecutive polls (%s): %s",
                kind, len(items), min(failure.consecutive for _, failure in items),
                ", ".join(details), _sample([tag for tag, _ in items]))


def _detail(status):
    return "no response" if status is None else f"status {status}"


def _by_kind(keys):
    grouped = {}
    for tag, kind in keys:
        grouped.setdefault(kind, []).append(tag)
    return grouped


def _sample(tags, limit=5):
    shown = ", ".join(sorted(tags)[:limit])
    return shown + (f" and {len(tags) - limit} more" if len(tags) > limit else "")
