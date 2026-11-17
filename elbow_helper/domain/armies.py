"""Decode CopyArmy category indices into named quantities, without inferring units.

The h/i/d/u/s sections and category bases agree with ClashPerk's public decoder:
https://github.com/clashperk/clashperk-bot/blob/main/src/commands/search/army.ts
Hero subfields p/e carry pets/equipment. The meaning of m is unverified; retain
its number. Named quantity mappings keep an army atomic when battle rows page.
"""

import re

from .army_units import EQUIPMENT, HEROES, PETS, SIEGE_IDS, SPELLS, TROOPS


_HERO = r"\d+(?:m\d+)?(?:p\d+)?(?:e\d+(?:_\d+)?)?"
_UNITS = r"\d+x\d+(?:-\d+x\d+)*"
_CODE = re.compile(rf"(?:h{_HERO}(?:-{_HERO})*)?(?:i{_UNITS})?(?:d{_UNITS})?"
                   rf"(?:u{_UNITS})?(?:s{_UNITS})?")
_HERO_PARTS = re.compile(r"(\d+)(?:m(\d+))?(?:p(\d+))?(?:e(\d+)(?:_(\d+))?)?")


def _quantity(target, table, identifier, count):
    name = table.get(identifier)
    if name is None:
        key = f"unknown_{identifier}"
        entry = target.setdefault(key, {"unknown": True, "id": identifier, "count": 0})
        entry["count"] += count
    else:
        target[name] = target.get(name, 0) + count


def decode_army(code):
    """Return name-to-count sections, with attachments under each named hero.

    Unknown IDs retain their number and count; unrecognized syntax retains the
    entire input explicitly as unparsed, rather than publishing a partial army.
    """
    if not isinstance(code, str) or not code or not _CODE.fullmatch(code):
        return {"unparsed_code": code}
    army = {}
    for section, body in re.findall(r"([hidus])([^hidus]+)", code):
        if section == "h":
            heroes = army.setdefault("heroes", {})
            for encoded in body.split("-"):
                identifier, mode, pet, first, second = _HERO_PARTS.fullmatch(encoded).groups()
                identifier = int(identifier)
                name = HEROES.get(identifier)
                hero = {"count": 1}
                if name is None:
                    hero.update(unknown=True, id=identifier)
                if mode is not None:
                    hero["m"] = int(mode)
                if pet is not None:
                    _quantity(hero.setdefault("pets", {}), PETS, int(pet), 1)
                for equipment in (first, second):
                    if equipment is not None:
                        _quantity(hero.setdefault("equipment", {}), EQUIPMENT, int(equipment), 1)
                heroes[name if name is not None else f"unknown_{identifier}"] = hero
        else:
            target = army.setdefault("clan_castle", {}) if section in ("i", "d") else army
            for encoded in body.split("-"):
                count, identifier = map(int, encoded.split("x"))
                spells = section in ("s", "d")
                category = "spells" if spells else "siege_machines" if identifier in SIEGE_IDS else "troops"
                _quantity(target.setdefault(category, {}), SPELLS if spells else TROOPS, identifier, count)
    return army
