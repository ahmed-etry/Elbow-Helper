from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Sequence

import discord

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL

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
SUPER_TROOP_NAMES_LOWER = {
    "super barbarian",
    "super archer",
    "super giant",
    "sneaky goblin",
    "super wall breaker",
    "rocket balloon",
    "super wizard",
    "inferno dragon",
    "super minion",
    "super valkyrie",
    "super witch",
    "ice hound",
    "super bowler",
    "super dragon",
    "super miner",
    "super hog rider",
    "super yeti",
}
SIEGE_MACHINE_NAMES_LOWER = {
    "wall wrecker",
    "battle blimp",
    "stone slammer",
    "siege barracks",
    "log launcher",
    "flame flinger",
    "battle drill",
    "troop launcher",
}
IGNORED_PLAN_TROOP_NAMES_LOWER = PET_NAMES_LOWER | SUPER_TROOP_NAMES_LOWER | SIEGE_MACHINE_NAMES_LOWER

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
]
DARK_SPELL_SHORT_NAMES = {
    "Earthquake Spell": "Quake",
    "Skeleton Spell": "Skeleton",
    "Bat Spell": "Bats",
    "Overgrowth Spell": "Overgrow",
}

PAGE_LABELS = ["Overview", "Hero Kit", "Army Kit"]
ARMY_SECTION_LABELS = {
    "troops": "Troops",
    "spells": "Spells",
    "unmapped": "Other",
}
MAX_FIELD_LEN = 1024


@dataclass(frozen=True)
class PlanningEmbeds:
    static_pages: List[discord.Embed]
    page_labels: List[str]
    army_embeds: dict[str, discord.Embed]

    def embed_for_page(self, page_index: int, army_section: str | None = None) -> discord.Embed:
        if page_index < len(self.static_pages):
            return self.static_pages[page_index]
        if army_section and army_section in self.army_embeds:
            return self.army_embeds[army_section]
        return next(iter(self.army_embeds.values()))

    def army_sections(self) -> list[str]:
        return list(self.army_embeds.keys())

    def default_army_section(self) -> str:
        return next(iter(self.army_embeds), "troops")


def _truncate_text(value: str | None, max_len: int = 900) -> str:
    text = (value or "").strip()
    if not text:
        return "--"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


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


def _wrap_entries(entries: Sequence[str], *, max_row_width: int) -> list[str]:
    rows: list[str] = []
    current = ""
    for entry in entries:
        candidate = entry if not current else f"{current} | {entry}"
        if current and len(candidate) > max_row_width:
            rows.append(current)
            current = entry
        else:
            current = candidate
    if current:
        rows.append(current)
    return rows


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


def _format_level_rows(
    names: Sequence[str],
    levels: dict[str, int],
    short_names: dict[str, str] | None = None,
    *,
    max_row_width: int = 40,
) -> str:
    if not names:
        return "None"
    entries = [
        f"{short_names.get(name, name) if short_names else name} {levels[name]}"
        for name in names
        if levels.get(name, 0) > 0
    ]
    return _join_clipped_lines(_wrap_entries(entries, max_row_width=max_row_width))


def _format_named_level_rows(
    entries: Sequence[tuple[str, int]],
    *,
    max_row_width: int = 44,
) -> str:
    if not entries:
        return "None"
    rendered = [f"{name} {level}" for name, level in entries]
    return _join_clipped_lines(_wrap_entries(rendered, max_row_width=max_row_width))


def _format_grouped_level_block(
    groups: Sequence[tuple[str, Sequence[str], dict[str, int], dict[str, str] | None]],
    *,
    max_row_width: int = 42,
) -> str:
    lines: list[str] = []
    for label, names, levels, short_names in groups:
        if not names:
            continue
        rendered = [
            f"{short_names.get(name, name) if short_names else name} {levels[name]}"
            for name in names
            if levels.get(name, 0) > 0
        ]
        if not rendered:
            continue
        lines.append(label)
        lines.extend(_wrap_entries(rendered, max_row_width=max_row_width))
        lines.append("")
    if lines and not lines[-1]:
        lines.pop()
    return _join_clipped_lines(lines)


def _apply_shared_embed_style(
    embed: discord.Embed,
    *,
    base_image: discord.Attachment,
    footer_text: str,
) -> discord.Embed:
    embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
    embed.set_image(url=base_image.url)
    embed.set_footer(text=footer_text)
    return embed


def _build_army_section_embed(
    player_name: str,
    base_image: discord.Attachment,
    *,
    description: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Army Kit: {player_name}",
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        timestamp=datetime.now(timezone.utc),
    )
    if description:
        embed.description = description
    return _apply_shared_embed_style(
        embed,
        base_image=base_image,
        footer_text="Army Kit • Page 3/3",
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
    interaction: discord.Interaction,
    player: dict,
    strategies: str,
    base_image: discord.Attachment,
) -> PlanningEmbeds:
    player_name = player.get("name") or "Unknown"
    player_tag = player.get("tag") or "--"
    th_level = player.get("townHallLevel", "N/A")

    hero_levels, ordered_heroes = _collect_hero_levels(player)
    pet_levels = _collect_pets(player)
    equipment_by_hero, unmapped_equipment = _collect_equipment(player, ordered_heroes)

    ordered_pets = [pet_name for pet_name in PET_ORDER if pet_name in pet_levels]
    ordered_pets.extend(sorted(pet_name for pet_name in pet_levels if pet_name not in set(PET_ORDER)))

    troop_levels = _collect_home_levels(
        player.get("troops", []),
        village_key="home",
        excluded_names=IGNORED_PLAN_TROOP_NAMES_LOWER,
    )
    ordered_troops, unmapped_troops = _ordered_names(
        troop_levels,
        ELIXIR_TROOP_ORDER + DARK_TROOP_ORDER,
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

    static_pages: List[discord.Embed] = []

    overview_embed = discord.Embed(
        title=f"Attack Plan Request: {player_name}",
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        timestamp=datetime.now(timezone.utc),
    )
    overview_embed.add_field(
        name="Account",
        value=(
            f"{player_name} (`{player_tag}`)\n"
            f"TH {th_level}\n"
            f"Requested by {interaction.user.mention}"
        ),
        inline=False,
    )
    overview_embed.add_field(name="Strategy Notes", value=_truncate_text(strategies, max_len=700), inline=False)
    overview_embed.add_field(
        name="Heroes",
        value=_format_level_rows(ordered_heroes, hero_levels, HERO_SHORT_NAMES, max_row_width=46),
        inline=False,
    )
    overview_embed.add_field(
        name="Pets",
        value=_format_level_rows(ordered_pets, pet_levels, PET_SHORT_NAMES, max_row_width=46),
        inline=False,
    )
    static_pages.append(
        _apply_shared_embed_style(
            overview_embed,
            base_image=base_image,
            footer_text="Overview • Page 1/3",
        )
    )

    hero_kit_embed = discord.Embed(
        title=f"Hero Kit: {player_name}",
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        timestamp=datetime.now(timezone.utc),
    )
    if ordered_heroes:
        for hero_name in ordered_heroes:
            hero_level = hero_levels.get(hero_name, 0)
            hero_kit_embed.add_field(
                name=f"{HERO_SHORT_NAMES.get(hero_name, hero_name)} Lv {hero_level}",
                value=_format_named_level_rows(equipment_by_hero.get(hero_name, []), max_row_width=52),
                inline=False,
            )
    else:
        hero_kit_embed.add_field(name="Heroes", value="No heroes found", inline=False)
    if unmapped_equipment:
        hero_kit_embed.add_field(
            name="Other Equipment",
            value=_format_named_level_rows(unmapped_equipment, max_row_width=52),
            inline=False,
        )
    static_pages.append(
        _apply_shared_embed_style(
            hero_kit_embed,
            base_image=base_image,
            footer_text="Hero Kit • Page 2/3",
        )
    )

    army_troops_embed = _build_army_section_embed(
        player_name,
        base_image,
        description="",
    )
    army_troops_embed.add_field(
        name="Troops",
        value=_format_grouped_level_block(
            [
                ("Elixir", elixir_troops, troop_levels, ELIXIR_TROOP_SHORT_NAMES),
                ("Dark", dark_troops, troop_levels, DARK_TROOP_SHORT_NAMES),
            ],
            max_row_width=48,
        ),
        inline=False,
    )
    army_spells_embed = _build_army_section_embed(
        player_name,
        base_image,
        description="",
    )
    army_spells_embed.add_field(
        name="Spells",
        value=_format_grouped_level_block(
            [
                ("Elixir", elixir_spells, spell_levels, ELIXIR_SPELL_SHORT_NAMES),
                ("Dark", dark_spells, spell_levels, DARK_SPELL_SHORT_NAMES),
            ],
            max_row_width=48,
        ),
        inline=False,
    )
    army_embeds: dict[str, discord.Embed] = {
        "troops": army_troops_embed,
        "spells": army_spells_embed,
    }
    if unmapped_troops or unmapped_spells:
        army_unmapped_embed = _build_army_section_embed(
            player_name,
            base_image,
            description="",
        )
        if unmapped_troops:
            army_unmapped_embed.add_field(
                name="Troops",
                value=_format_level_rows(unmapped_troops, troop_levels, max_row_width=34),
                inline=False,
            )
        if unmapped_spells:
            army_unmapped_embed.add_field(
                name="Spells",
                value=_format_level_rows(unmapped_spells, spell_levels, max_row_width=34),
                inline=False,
            )
        army_embeds["unmapped"] = army_unmapped_embed

    return PlanningEmbeds(
        static_pages=static_pages,
        page_labels=PAGE_LABELS,
        army_embeds=army_embeds,
    )
