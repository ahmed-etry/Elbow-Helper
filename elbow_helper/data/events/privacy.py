"""Member deletion in feature-owned operational documents."""
import json

from elbow_helper.infrastructure.persistence.documents import encode

ACTOR_FIELDS = {"actor_discord_id", "updated_by_id", "last_actor_id", "completed_by_id"}
NAME_FIELDS = {"actor_display", "updated_by_name", "completed_by_name"}


def _scrub(value, member):
    if isinstance(value, list):
        return [_scrub(item, member) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    actor = any(result.get(field) == member for field in ACTOR_FIELDS)
    for key, item in result.items():
        if key in ACTOR_FIELDS and item == member:
            result[key] = None
        elif actor and key in NAME_FIELDS:
            result[key] = None
        elif key in ("recipient_ids", "managed") and isinstance(item, list):
            result[key] = [identifier for identifier in item if identifier != member]
        elif key == "lineup" and isinstance(item, dict):
            result[key] = {tag: identifier for tag, identifier in item.items() if identifier != member}
        else:
            result[key] = _scrub(item, member)
    return result


def delete_document_member(connection, member):
    for name in ("wars", "cwl_bonus_rules", "cwl_bonus_previous", "cwl_bonus_audit", "cwl_bonus_boards"):
        row = connection.execute("SELECT payload_json FROM feature_documents WHERE name=?", (name,)).fetchone()
        if row:
            connection.execute("UPDATE feature_documents SET payload_json=? WHERE name=?",
                               (encode(_scrub(json.loads(row[0]), member)), name))
    connection.execute("UPDATE clan_health_config_audit SET actor_discord_id=0,actor_display='' "
                       "WHERE actor_discord_id=?", (member,))
