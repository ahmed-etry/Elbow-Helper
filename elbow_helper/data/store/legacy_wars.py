"""Old war identities mapped once to the record's wars; filled by the later copy-in."""


SOURCES = {"clan_health", "war_cache"}


def map_legacy_war(connection, source, legacy_id, clan_code, war_id, *, label=None, capture_state=None,
                   captured_at=None, at):
    """Accept an old ID once. Repeating the same mapping adds nothing; a different one is refused."""
    if source not in SOURCES or not isinstance(legacy_id, str) or not legacy_id or not isinstance(clan_code, str):
        raise ValueError("invalid_legacy_war")
    if not connection.execute("SELECT 1 FROM wars WHERE id=?", (war_id,)).fetchone():
        raise ValueError("unknown_war")
    evidence = (war_id, label, capture_state, captured_at)
    old = connection.execute(
        "SELECT war_id,legacy_label,capture_state,captured_at FROM war_legacy_ids "
        "WHERE source=? AND legacy_id=? AND clan_code=?", (source, legacy_id, clan_code)).fetchone()
    if old is not None:
        if tuple(old) != evidence:
            raise ValueError("legacy_war_conflict")
        return False
    connection.execute("INSERT INTO war_legacy_ids(source,legacy_id,clan_code,war_id,legacy_label,capture_state,"
                       "captured_at,mapped_at) VALUES (?,?,?,?,?,?,?,?)", (source, legacy_id, clan_code, *evidence, at))
    return True
