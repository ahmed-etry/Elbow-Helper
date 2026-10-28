"""Correct literal message counts in generated social text without another model call."""

import re


_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
          "fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()
_TENS = {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_COUNTS = {**dict(zip(_SMALL, range(21))), **_TENS}
_NUMBER = r"(?:\d+|" + "|".join(_COUNTS) + r")(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?"
_CLAIM = re.compile(r"\b(?P<number>" + _NUMBER + r")(?P<separator>\s+|-)(?P<unit>letters?|digits?|numbers?|characters?|words?)\b",
                    re.IGNORECASE)
_QUOTED_SUBJECT = re.compile(r"(?:`([^`]+)`|\"([^\"]+)\"|“([^”]+)”|'([^']+)')\s+(?:has|is|contains)\s+(?:only\s+|just\s+)?$",
                             re.IGNORECASE)
_MESSAGE_SUBJECT = re.compile(r"\b(?:your (?:message|text|reply)|you (?:typed|sent|wrote)|that(?:'s| is)|this(?:'s| is))\b",
                              re.IGNORECASE)


def _value(text):
    if text.isdecimal():
        return int(text)
    return sum(_COUNTS.get(part, 0) for part in text.casefold().replace("-", " ").split())


def _count(text, unit):
    if unit.startswith("letter"):
        return sum(character.isalpha() for character in text)
    if unit.startswith("digit"):
        return sum(character.isdecimal() for character in text)
    if unit.startswith("number"):
        # Numbers are maximal runs of decimal digits, not individual digits.
        return sum(character.isdecimal() and (index == 0 or not text[index - 1].isdecimal())
                   for index, character in enumerate(text))
    if unit.startswith("word"):
        return len(text.split())
    return len(text)


def correct_social_counts(answer, message):
    """Only match generated claims; the asker's message is counted as literal data."""
    def correct(match):
        prefix = answer[:match.start()]
        subject = _QUOTED_SUBJECT.search(prefix)
        clause = re.split(r"[.!?;,:\n]", prefix)[-1].strip().casefold()
        literal_subject = any(clause == message.casefold() + ending for ending in (" is", " has", " contains"))
        if (not subject and not literal_subject and clause not in ("", "only", "just")
                and not _MESSAGE_SUBJECT.search(clause)):
            return match[0]
        literal = next(filter(None, subject.groups())) if subject else message
        unit = match["unit"].casefold()
        actual = _count(literal, unit)
        if _value(match["number"]) == actual:
            return match[0]
        number = str(actual) if match["number"].isdecimal() or actual >= len(_SMALL) else _SMALL[actual]
        if match["number"][0].isupper():
            number = number.capitalize()
        noun = unit.rstrip("s") + ("s" if actual != 1 and match["separator"] != "-" else "")
        return number + match["separator"] + noun
    return _CLAIM.sub(correct, answer)
