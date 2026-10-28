"""One cursor identity for retained family snapshots and bounded delivery."""
from base64 import urlsafe_b64encode
from hashlib import sha256
import json


def family_cursor(report_id, requester_id, selection, offset):
    digest = sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()
    return urlsafe_b64encode(json.dumps(
        [report_id, requester_id, digest, offset], separators=(',', ':')).encode()).decode()
