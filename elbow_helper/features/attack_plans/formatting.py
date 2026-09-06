from dataclasses import dataclass
from typing import Dict, Mapping, Sequence

import discord

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL

from .unit_levels import town_hall_max_level

HERO_ORDER: list[tuple[str, str]] = [
    ("Barbarian King", "King"),
    ("Archer Queen", "Queen"),
    ("Minion Prince", "Prince"),
    ("Grand Warden", "Warden"),
    ("Royal Champion", "RC"),
    ("Dragon Duke", "Duke"),
]
HERO_SHORT_NAMES = {name: short for name, short in HERO_ORDER}

PET_ORDER = [
    "L.A.S.S.I",
    "Electro Owl",
    "Mighty Yak",
    "Unicorn",
    "Frosty",
    "Diggy",
    "Poison Lizard",
    "Phoenix",
    "Spirit Fox",
    "Angry Jelly",
    "Sneezy",
    "Greedy Raven",
]
PET_SHORT_NAMES = {
    "L.A.S.S.I": "LASSI",
    "Electro Owl": "Owl",
    "Mighty Yak": "Yak",
    "Poison Lizard": "Lizard",
    "Spirit Fox": "Fox",
    "Angry Jelly": "Jelly",
    "Greedy Raven": "Raven",
}
PET_NAMES_LOWER = {name.lower() for name in PET_ORDER}

HERO_EQUIPMENT_ORDER = {
    "Barbarian King": [
        "Barbarian Puppet",
        "Rage Vial",
        "Earthquake Boots",
        "Vampstache",
        "Giant Gauntlet",
        "Spiky Ball",
        "Snake Bracelet",
        "Stick Horse",
    ],
    "Archer Queen": [
        "Archer Puppet",
        "Invisibility Vial",
        "Giant Arrow",
        "Healer Puppet",
        "Frozen Arrow",
        "Magic Mirror",
        "Action Figure",
        "Monolith Arrow",
    ],
    "Minion Prince": [
        "Henchmen Puppet",
        "Dark Orb",
        "Metal Pants",
        "Noble Iron",
        "Dark Crown",
        "Meteor Staff",
    ],
    "Grand Warden": [
        "Eternal Tome",
        "Life Gem",
        "Rage Gem",
        "Healing Tome",
        "Fireball",
        "Lavaloon Puppet",
        "Heroic Torch",
    ],
    "Royal Champion": [
        "Royal Gem",
        "Seeking Shield",
        "Hog Rider Puppet",
        "Haste Vial",
        "Rocket Spear",
        "Electro Boots",
        "Frost Flake",
    ],
    "Dragon Duke": [
        "Fire Heart",
        "Flame Blower",
        "Stun Blaster",
        "Electro Fangs",
        "Rocket Backpack",
        "Revenge Deck",
    ],
}
HERO_EQUIPMENT_LOWER = {
    hero_name: {equipment_name.lower() for equipment_name in names}
    for hero_name, names in HERO_EQUIPMENT_ORDER.items()
}
HERO_EQUIPMENT_INDEX = {
    hero_name: {equipment_name: idx for idx, equipment_name in enumerate(names)}
    for hero_name, names in HERO_EQUIPMENT_ORDER.items()
}

ELIXIR_TROOP_ORDER = [
    "Barbarian",
    "Archer",
    "Giant",
    "Goblin",
    "Wall Breaker",
    "Balloon",
    "Wizard",
    "Healer",
    "Dragon",
    "P.E.K.K.A",
    "Baby Dragon",
    "Miner",
    "Electro Dragon",
    "Yeti",
    "Dragon Rider",
    "Electro Titan",
    "Root Rider",
    "Thrower",
    "Meteor Golem",
]
ELIXIR_TROOP_SHORT_NAMES = {
    "Wall Breaker": "WB",
    "Baby Dragon": "Baby Drag",
    "Electro Dragon": "E-Drag",
    "Electro Titan": "E-Titan",
    "Meteor Golem": "Meteor",
}

DARK_TROOP_ORDER = [
    "Minion",
    "Hog Rider",
    "Valkyrie",
    "Golem",
    "Witch",
    "Lava Hound",
    "Bowler",
    "Ice Golem",
    "Headhunter",
    "Apprentice Warden",
    "Druid",
    "Furnace",
    "Ruin Witch",
]
DARK_TROOP_SHORT_NAMES = {
    "Hog Rider": "Hog",
    "Lava Hound": "Hound",
    "Headhunter": "HH",
    "Apprentice Warden": "App Ward",
}

ELIXIR_SPELLS_ORDER = [
    "Lightning Spell",
    "Healing Spell",
    "Rage Spell",
    "Jump Spell",
    "Freeze Spell",
    "Clone Spell",
    "Invisibility Spell",
    "Recall Spell",
    "Revive Spell",
    "Totem Spell",
]
ELIXIR_SPELL_SHORT_NAMES = {
    "Lightning Spell": "Lightning",
    "Healing Spell": "Heal",
    "Invisibility Spell": "Invis",
}

DARK_SPELLS_ORDER = [
    "Poison Spell",
    "Earthquake Spell",
    "Haste Spell",
    "Skeleton Spell",
    "Bat Spell",
    "Overgrowth Spell",
    "Ice Block Spell",
    "Angry Spell",
]
DARK_SPELL_SHORT_NAMES = {
    "Earthquake Spell": "Quake",
    "Skeleton Spell": "Skeleton",
    "Bat Spell": "Bats",
    "Overgrowth Spell": "Overgrow",
}

SIEGE_MACHINE_ORDER = [
    "Wall Wrecker",
    "Battle Blimp",
    "Stone Slammer",
    "Siege Barracks",
    "Log Launcher",
    "Flame Flinger",
    "Battle Drill",
    "Troop Launcher",
    "Sky Wagon",
]

SUPER_TROOP_ORDER = [
    "Super Barbarian",
    "Super Archer",
    "Super Giant",
    "Sneaky Goblin",
    "Super Wall Breaker",
    "Rocket Balloon",
    "Super Wizard",
    "Inferno Dragon",
    "Super Minion",
    "Super Valkyrie",
    "Super Witch",
    "Ice Hound",
    "Super Bowler",
    "Super Dragon",
    "Super Miner",
    "Super Hog Rider",
    "Super Yeti",
]

MAX_FIELD_LEN = 1024


@dataclass(frozen=True)
class PlanningEmbeds:
    pages: list[discord.Embed]
    overview_extras: tuple[discord.Embed, ...] = ()

    def embeds_for_page(self, page_index: int) -> list[discord.Embed]:
        embeds = [self.pages[page_index]]
        if page_index == 0:
            embeds.extend(self.overview_extras)
        return embeds


def _truncate_text(value: str | None, max_len: int = 900) -> str:
    text = (value or "").strip()
    if not text:
        return "--"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def required_plan_unit_names() -> tuple[str, ...]:
    """Return every Home Village unit with an attack-plan application emoji."""

    names = (
        *(hero_name for hero_name, _ in HERO_ORDER),
        *PET_ORDER,
        *(equipment for names in HERO_EQUIPMENT_ORDER.values() for equipment in names),
        *ELIXIR_TROOP_ORDER,
        *DARK_TROOP_ORDER,
        *ELIXIR_SPELLS_ORDER,
        *DARK_SPELLS_ORDER,
    )
    return tuple(dict.fromkeys(names))


def _collect_home_levels(
    entries: Sequence[dict],
    *,
    village_key: str,
    excluded_names: set[str] | None = None,
) -> dict[str, int]:
    excluded = excluded_names or set()
    levels: dict[str, int] = {}
    for entry in entries:
        name = entry.get("name")
        level = int(entry.get("level", 0) or 0)
        if not name or level <= 0 or entry.get("village") != village_key:
            continue
        if name.lower() in excluded:
            continue
        levels[name] = max(levels.get(name, 0), level)
    return levels


def _ordered_names(levels: dict[str, int], preferred_order: Sequence[str]) -> tuple[list[str], list[str]]:
    preferred = set(preferred_order)
    ordered = [name for name in preferred_order if levels.get(name, 0) > 0]
    other = sorted(name for name, level in levels.items() if level > 0 and name not in preferred)
    return ordered, other


def _wrap_entries(entries: Sequence[str], *, entries_per_row: int = 4) -> list[str]:
    return [
        " ".join(entries[index : index + entries_per_row])
        for index in range(0, len(entries), entries_per_row)
    ]


def _join_clipped_lines(lines: Sequence[str]) -> str:
    if not lines:
        return "None"
    text = "\n".join(lines)
    if len(text) <= MAX_FIELD_LEN:
        return text

    clipped_lines: list[str] = []
    current_len = 0
    for line in lines:
        projected = current_len + len(line) + (1 if clipped_lines else 0)
        if projected > MAX_FIELD_LEN - 16:
            break
        clipped_lines.append(line)
        current_len = projected

    hidden_count = len(lines) - len(clipped_lines)
    if hidden_count > 0:
        clipped_lines.append(f"...and {hidden_count} more")
    return "\n".join(clipped_lines)


def _format_unit_level(
    name: str,
    level: int,
    max_levels: Mapping[str, int],
    short_names: Mapping[str, str] | None,
    emoji_tokens: Mapping[str, str],
) -> str:
    label = emoji_tokens.get(name)
    if label is None:
        label = short_names.get(name, name) if short_names else name
    current_text = str(level).rjust(2)
    max_text = str(max(max_levels.get(name, level), level)).ljust(2)
    return f"{label}\u00a0`\u200e{current_text}/{max_text}\u200f`"


def _format_level_rows(
    names: Sequence[str],
    levels: dict[str, int],
    short_names: Mapping[str, str] | None = None,
    *,
    max_levels: Mapping[str, int] | None = None,
    emoji_tokens: Mapping[str, str] | None = None,
) -> str:
    if not names:
        return "None"
    tokens = emoji_tokens or {}
    caps = max_levels or levels
    entries = [
        _format_unit_level(name, levels[name], caps, short_names, tokens)
        for name in names
        if levels.get(name, 0) > 0
    ]
    return _join_clipped_lines(_wrap_entries(entries))


def _format_named_level_rows(
    entries: Sequence[tuple[str, int]],
    *,
    max_levels: Mapping[str, int] | None = None,
    emoji_tokens: Mapping[str, str] | None = None,
) -> str:
    if not entries:
        return "None"
    tokens = emoji_tokens or {}
    caps = max_levels or {name: level for name, level in entries}
    rendered = [
        _format_unit_level(name, level, caps, None, tokens)
        for name, level in entries
    ]
    return _join_clipped_lines(_wrap_entries(rendered))


def _apply_base_image(
    embed: discord.Embed,
    *,
    base_image: discord.Attachment,
) -> discord.Embed:
    embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
    embed.set_image(url=base_image.url)
    return embed


def _build_army_embed(
    player_name: str,
    town_hall_level: int | str,
    base_image: discord.Attachment,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Army Kit: {player_name} • TH{town_hall_level}",
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
    )
    return _apply_base_image(
        embed,
        base_image=base_image,
    )


def _collect_hero_levels(player: dict) -> tuple[dict[str, int], list[str]]:
    hero_levels: Dict[str, int] = {
        hero.get("name"): hero.get("level", 0)
        for hero in player.get("heroes", [])
        if hero.get("name")
        and hero.get("level", 0) > 0
        and hero.get("village") in (None, "home")
    }
    preferred_names = [hero_name for hero_name, _ in HERO_ORDER]
    preferred = set(preferred_names)
    ordered_heroes = [name for name in preferred_names if name in hero_levels]
    ordered_heroes.extend(sorted(name for name in hero_levels if name not in preferred))
    return hero_levels, ordered_heroes


def _collect_pets(player: dict) -> dict[str, int]:
    pet_levels: dict[str, int] = {}

    for pet in player.get("pets", []):
        name = pet.get("name")
        level = int(pet.get("level", 0) or 0)
        if name and level > 0:
            pet_levels[name] = max(pet_levels.get(name, 0), level)

    for troop in player.get("troops", []):
        name = troop.get("name")
        level = int(troop.get("level", 0) or 0)
        if name and level > 0 and name.lower() in PET_NAMES_LOWER:
            pet_levels[name] = max(pet_levels.get(name, 0), level)

    return pet_levels


def _resolve_equipment_owner(entry: dict, ordered_heroes: Sequence[str]) -> str | None:
    for key in ("hero", "heroName", "owner", "belongsTo"):
        owner_name = entry.get(key)
        if isinstance(owner_name, str) and owner_name in ordered_heroes:
            return owner_name

    equipment_name = str(entry.get("name") or "").lower()
    for hero_name, equipment_names in HERO_EQUIPMENT_LOWER.items():
        if equipment_name in equipment_names:
            return hero_name
    return None


def _collect_equipment(
    player: dict,
    ordered_heroes: Sequence[str],
) -> tuple[dict[str, list[tuple[str, int]]], list[tuple[str, int]]]:
    equipment_by_hero: dict[str, list[tuple[str, int]]] = {hero_name: [] for hero_name in ordered_heroes}
    unmapped_equipment: list[tuple[str, int]] = []

    for equipment in player.get("heroEquipment", []):
        name = equipment.get("name") or "Equipment"
        level = int(equipment.get("level", 0) or 0)
        owner = _resolve_equipment_owner(equipment, ordered_heroes)
        if owner and owner in equipment_by_hero:
            equipment_by_hero[owner].append((name, level))
        else:
            unmapped_equipment.append((name, level))

    for hero_name in equipment_by_hero:
        preferred_index = HERO_EQUIPMENT_INDEX.get(hero_name, {})
        equipment_by_hero[hero_name].sort(
            key=lambda item: (preferred_index.get(item[0], len(preferred_index) + 1), item[0])
        )
    unmapped_equipment.sort(key=lambda item: item[0])
    return equipment_by_hero, unmapped_equipment


def build_planning_embeds(
    player: dict,
    thinking: str,
    strategy_image: discord.Attachment,
    base_image: discord.Attachment,
    *,
    emoji_tokens: Mapping[str, str] | None = None,
) -> PlanningEmbeds:
    tokens = emoji_tokens or {}
    player_name = player.get("name") or "Unknown"
    th_level = player.get("townHallLevel", "N/A")
    try:
        town_hall_level = int(th_level)
    except (TypeError, ValueError):
        town_hall_level = 0

    hero_levels, ordered_heroes = _collect_hero_levels(player)
    pet_levels = _collect_pets(player)
    equipment_by_hero, unmapped_equipment = _collect_equipment(player, ordered_heroes)

    ordered_pets = [pet_name for pet_name in PET_ORDER if pet_name in pet_levels]
    ordered_pets.extend(sorted(pet_name for pet_name in pet_levels if pet_name not in set(PET_ORDER)))

    troop_levels = _collect_home_levels(
        player.get("troops", []),
        village_key="home",
        excluded_names=PET_NAMES_LOWER,
    )
    ordered_troops, unmapped_troops = _ordered_names(
        troop_levels,
        ELIXIR_TROOP_ORDER
        + DARK_TROOP_ORDER
        + SIEGE_MACHINE_ORDER
        + SUPER_TROOP_ORDER,
    )
    elixir_troops = [name for name in ordered_troops if name in ELIXIR_TROOP_ORDER]
    dark_troops = [name for name in ordered_troops if name in DARK_TROOP_ORDER]

    spell_levels = _collect_home_levels(player.get("spells", []), village_key="home")
    ordered_spells, unmapped_spells = _ordered_names(
        spell_levels,
        ELIXIR_SPELLS_ORDER + DARK_SPELLS_ORDER,
    )
    elixir_spells = [name for name in ordered_spells if name in ELIXIR_SPELLS_ORDER]
    dark_spells = [name for name in ordered_spells if name in DARK_SPELLS_ORDER]

    current_levels = {
        **hero_levels,
        **pet_levels,
        **troop_levels,
        **spell_levels,
        **{
            name: level
            for equipment in equipment_by_hero.values()
            for name, level in equipment
        },
        **dict(unmapped_equipment),
    }
    hall_max_levels = {
        name: town_hall_max_level(name, town_hall_level, level)
        for name, level in current_levels.items()
    }

    pages: list[discord.Embed] = []

    overview_embed = discord.Embed(
        title=f"Attack Plan: {player_name} • TH{th_level}",
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
    )
    overview_embed.add_field(name="Thinking", value=_truncate_text(thinking, max_len=700), inline=False)
    overview_embed.add_field(
        name="Heroes",
        value=_format_level_rows(
            ordered_heroes,
            hero_levels,
            HERO_SHORT_NAMES,
            max_levels=hall_max_levels,
            emoji_tokens=tokens,
        ),
        inline=False,
    )
    overview_embed.add_field(
        name="Pets",
        value=_format_level_rows(
            ordered_pets,
            pet_levels,
            PET_SHORT_NAMES,
            max_levels=hall_max_levels,
            emoji_tokens=tokens,
        ),
        inline=False,
    )
    pages.append(
        _apply_base_image(
            overview_embed,
            base_image=strategy_image,
        )
    )
    overview_base_embed = discord.Embed(color=discord.Color(DEFAULT_EMBED_COLOR_HEX))
    overview_base_embed.set_image(url=base_image.url)

    hero_kit_embed = discord.Embed(
        title=f"Hero Kit: {player_name} • TH{th_level}",
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
    )
    if ordered_heroes:
        for hero_name in ordered_heroes:
            hero_level = hero_levels.get(hero_name, 0)
            hero_label = _format_unit_level(
                hero_name,
                hero_level,
                hall_max_levels,
                HERO_SHORT_NAMES,
                tokens,
            )
            hero_kit_embed.add_field(
                name=hero_label,
                value=_format_named_level_rows(
                    equipment_by_hero.get(hero_name, []),
                    max_levels=hall_max_levels,
                    emoji_tokens=tokens,
                ),
                inline=False,
            )
    else:
        hero_kit_embed.add_field(name="Heroes", value="No heroes found", inline=False)
    if unmapped_equipment:
        hero_kit_embed.add_field(
            name="Other Equipment",
            value=_format_named_level_rows(
                unmapped_equipment,
                max_levels=hall_max_levels,
                emoji_tokens=tokens,
            ),
            inline=False,
        )
    pages.append(
        _apply_base_image(
            hero_kit_embed,
            base_image=base_image,
        )
    )

    army_embed = _build_army_embed(
        player_name,
        th_level,
        base_image,
    )
    army_embed.add_field(
        name="Elixir Troops",
        value=_format_level_rows(
            elixir_troops,
            troop_levels,
            ELIXIR_TROOP_SHORT_NAMES,
            max_levels=hall_max_levels,
            emoji_tokens=tokens,
        ),
        inline=False,
    )
    army_embed.add_field(
        name="Dark Elixir Troops",
        value=_format_level_rows(
            dark_troops,
            troop_levels,
            DARK_TROOP_SHORT_NAMES,
            max_levels=hall_max_levels,
            emoji_tokens=tokens,
        ),
        inline=False,
    )
    army_embed.add_field(
        name="Elixir Spells",
        value=_format_level_rows(
            elixir_spells,
            spell_levels,
            ELIXIR_SPELL_SHORT_NAMES,
            max_levels=hall_max_levels,
            emoji_tokens=tokens,
        ),
        inline=False,
    )
    army_embed.add_field(
        name="Dark Spells",
        value=_format_level_rows(
            dark_spells,
            spell_levels,
            DARK_SPELL_SHORT_NAMES,
            max_levels=hall_max_levels,
            emoji_tokens=tokens,
        ),
        inline=False,
    )
    if unmapped_troops or unmapped_spells:
        if unmapped_troops:
            army_embed.add_field(
                name="Other Troops",
                value=_format_level_rows(
                    unmapped_troops,
                    troop_levels,
                    max_levels=hall_max_levels,
                    emoji_tokens=tokens,
                ),
                inline=False,
            )
        if unmapped_spells:
            army_embed.add_field(
                name="Other Spells",
                value=_format_level_rows(
                    unmapped_spells,
                    spell_levels,
                    max_levels=hall_max_levels,
                    emoji_tokens=tokens,
                ),
                inline=False,
            )
    pages.append(army_embed)

    return PlanningEmbeds(pages=pages, overview_extras=(overview_base_embed,))
