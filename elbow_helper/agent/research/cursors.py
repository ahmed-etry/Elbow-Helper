"""Canonical scope identity shared by research cursor formats."""

import hashlib
import json


def scope_digest(scope):
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
