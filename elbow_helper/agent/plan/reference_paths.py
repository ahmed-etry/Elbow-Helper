"""Typed result paths shared by validation and execution."""

PATH_CONTRACT = {
    "length": [1, 8], "key": "nonempty string",
    "index": "nonnegative integer or decimal string on lists", "expand": "*",
}


def valid_path(path):
    return (isinstance(path, list)
            and PATH_CONTRACT["length"][0] <= len(path) <= PATH_CONTRACT["length"][1]
            and all(type(part) is int and part >= 0
                    or isinstance(part, str) and bool(part)
                    for part in path))


def path_key(source, part):
    """Decimal strings index lists; mappings retain their original string keys."""
    return int(part) if isinstance(source, list) and isinstance(part, str) and part.isdecimal() else part
