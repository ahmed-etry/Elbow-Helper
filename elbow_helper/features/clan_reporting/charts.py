"""Chart rendering for monthly clan war summaries."""

from __future__ import annotations

import io
from typing import Optional

import discord

from .config import WAR_CHART_FILENAME


try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, Rectangle

    MATPLOTLIB_AVAILABLE = True
except (ImportError, ModuleNotFoundError, OSError, RuntimeError):
    MATPLOTLIB_AVAILABLE = False
    plt = None
    FancyBboxPatch = None
    Rectangle = None


def build_war_summary_chart(
    clan_name: str,
    month_label: str,
    wins: int,
    losses: int,
    ties: int,
) -> Optional[discord.File]:
    """Render the monthly war summary chart when matplotlib is available."""
    if not MATPLOTLIB_AVAILABLE or plt is None:
        return None

    labels = ["Wins", "Ties", "Losses"]
    values = [wins, ties, losses]
    colors = ["#3ba55c", "#faa81a", "#ed4245"]
    total = wins + losses + ties
    win_rate_denominator = wins + losses
    win_rate = (wins / win_rate_denominator * 100) if win_rate_denominator else 0.0

    fig, ax = plt.subplots(figsize=(7.2, 4.05), dpi=160)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("none")
    ax.set_zorder(2)
    ax.patch.set_alpha(0)

    shadow = FancyBboxPatch(
        (0.05, 0.05),
        0.9,
        0.88,
        boxstyle="round,pad=0.02,rounding_size=0.03",
        transform=fig.transFigure,
        facecolor="#d9d9d9",
        edgecolor="none",
        alpha=0.35,
        zorder=0,
    )
    card = FancyBboxPatch(
        (0.04, 0.06),
        0.9,
        0.88,
        boxstyle="round,pad=0.02,rounding_size=0.03",
        transform=fig.transFigure,
        facecolor="#ffffff",
        edgecolor="#e5e5e5",
        linewidth=1.0,
        zorder=1,
    )
    fig.add_artist(shadow)
    fig.add_artist(card)

    header = Rectangle(
        (0.04, 0.83),
        0.9,
        0.1,
        transform=fig.transFigure,
        facecolor="#f5f5f5",
        edgecolor="none",
        zorder=4,
    )
    fig.add_artist(header)
    fig.text(0.5, 0.885, f"{clan_name} - {month_label}", ha="center", va="center", color="#222222", fontsize=11, zorder=5)
    fig.text(0.9, 0.885, f"Total {total}", ha="right", va="center", color="#333333", fontsize=9, zorder=5)
    fig.text(0.9, 0.85, f"Win rate {win_rate:.1f}%", ha="right", va="center", color="#666666", fontsize=9, zorder=5)

    ax.set_position([0.12, 0.18, 0.76, 0.64])

    bar_positions = [0.0, 1.4, 2.8]
    bars = ax.bar(bar_positions, values, width=0.55, color=colors)
    ax.set_xticks(bar_positions, labels)
    ax.tick_params(axis="x", colors="#222222", labelsize=9)
    ax.tick_params(axis="y", colors="#222222", labelsize=9)
    ax.grid(axis="y", color="#e0e0e0", linewidth=0.7, alpha=0.9)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)

    max_value = max(values) if max(values) > 0 else 1
    ax.set_ylim(0, max_value * 1.35)
    ax.set_yticks(list(range(0, int(max_value) + 1)))

    for bar, value in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + (max_value * 0.05),
            str(value),
            ha="center",
            va="bottom",
            color="#222222",
            fontsize=9,
        )

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buffer.seek(0)
    return discord.File(buffer, filename=WAR_CHART_FILENAME)
