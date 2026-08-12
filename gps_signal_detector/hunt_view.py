"""Rendu du mode traque, sous forme de lignes pures.

Le rendu ne dépend pas de curses : il produit une liste de lignes typées,
que l'afficheur se contente de peindre. Cela rend l'interface testable sans
terminal — et permet de la relire à froid pour vérifier qu'elle ne raconte
rien de faux.
"""

from __future__ import annotations

from dataclasses import dataclass

from .live_state import (
    HISTORY_WINDOW_S,
    SIGNAL_CEILING,
    SIGNAL_FLOOR,
    LiveDevice,
    LiveState,
    bucketed_series,
    proximity_band,
    signal_ratio,
    trend_label,
)

BLOCKS = "▁▂▃▄▅▆▇█"

# L'échelle est définie dans live_state : terminal et interface web doivent
# placer le curseur au même endroit pour la même mesure.
GAUGE_FLOOR = SIGNAL_FLOOR
GAUGE_CEILING = SIGNAL_CEILING


@dataclass(frozen=True)
class Line:
    """Une ligne d'affichage et son style."""

    text: str
    style: str = ""


def gauge(ratio: float, width: int, marker_ratio: float | None = None) -> str:
    """Barre horizontale, avec un repère facultatif (meilleur signal)."""
    if width <= 0:
        return ""
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    cells = ["█"] * filled + ["░"] * (width - filled)
    if marker_ratio is not None:
        index = int(round(max(0.0, min(1.0, marker_ratio)) * width))
        index = max(0, min(width - 1, index))
        cells[index] = "┃"
    return "".join(cells)


def gauge_ratio(rssi: float) -> float:
    """Position d'une puissance sur la jauge, entre 0 et 1."""
    return signal_ratio(rssi)


def sparkline(
    samples,
    now: float,
    window_s: float,
    width: int,
    low: float | None = None,
    high: float | None = None,
) -> str:
    """Courbe compacte des mesures récentes.

    Les trames n'arrivent pas à cadence régulière : on découpe la fenêtre en
    colonnes de temps égales et on prend la médiane de chacune. Les colonnes
    sans mesure restent vides plutôt que d'être interpolées — un trou est
    une information (l'appareil s'est tu).
    """
    if width <= 0:
        return ""
    values = bucketed_series(samples, now, window_s, width)
    present = [value for value in values if value is not None]
    if not present:
        return " " * width

    floor = low if low is not None else min(present)
    ceiling = high if high is not None else max(present)
    span = (ceiling - floor) or 1.0

    cells = []
    for value in values:
        if value is None:
            cells.append(" ")
            continue
        level = int(round((value - floor) / span * (len(BLOCKS) - 1)))
        cells.append(BLOCKS[max(0, min(len(BLOCKS) - 1, level))])
    return "".join(cells)


def _identity_line(device: LiveDevice) -> str:
    parts = [device.address]
    if device.signature is not None:
        parts.append(device.signature.name)
    else:
        parts.append("famille inconnue")
    if device.separated:
        parts.append("séparé du propriétaire")
    if device.battery:
        parts.append(f"batterie {device.battery}")
    return " · ".join(parts)


def render_hunt(
    device: LiveDevice,
    now: float,
    width: int = 80,
    samples_per_device: float | None = None,
) -> list[Line]:
    """Écran de traque d'un appareil."""
    inner = max(40, width - 2)
    lines: list[Line] = []

    lines.append(Line(f" TRAQUE · {device.label}"[:inner], "title"))
    lines.append(Line(f" {_identity_line(device)}"[:inner], "dim"))
    lines.append(Line(" " + "─" * (inner - 1), "dim"))
    lines.append(Line(""))

    smoothed = device.smoothed(now)
    if smoothed is None:
        lines.append(Line("     Aucune mesure sur les dernières secondes.", "alert"))
        lines.append(
            Line("     L'appareil s'est tu — rapprochez-vous ou attendez.", "dim")
        )
        lines.append(Line(""))
    else:
        bar_width = max(20, inner - 26)
        marker = gauge_ratio(device.best_rssi) if device.best_rssi is not None else None
        lines.append(
            Line(
                f"     {smoothed:6.0f} dBm   "
                f"{gauge(gauge_ratio(smoothed), bar_width, marker)}",
                "value",
            )
        )
        lines.append(Line(""))

        delta = device.trend(now)
        label, style = trend_label(delta)
        variation = f"{delta:+.1f} dB" if delta is not None else ""
        lines.append(Line(f"     {label}        {variation}", style))
        lines.append(Line(""))

        band, hint = proximity_band(smoothed)
        lines.append(Line(f"     {band} — {hint}", "value"))
        lines.append(Line("     (ordre de grandeur : le RSSI ne mesure pas une distance)", "dim"))
        lines.append(Line(""))

    # Historique récent.
    curve_width = max(20, inner - 4)
    window_values = device.values_between(now - HISTORY_WINDOW_S, now)
    lines.append(Line(f" {int(HISTORY_WINDOW_S)} dernières secondes", "dim"))
    lines.append(
        Line("  " + sparkline(device.samples, now, HISTORY_WINDOW_S, curve_width), "curve")
    )
    if window_values:
        left = f"bas {min(window_values)} dBm"
        right = f"haut {max(window_values)} dBm"
        padding = max(1, curve_width - len(left) - len(right))
        lines.append(Line("  " + left + " " * padding + right, "dim"))
    lines.append(Line(""))

    if device.best_rssi is not None:
        since = now - device.best_at
        lines.append(
            Line(
                f" Meilleur de la session : {device.best_rssi:.0f} dBm "
                f"(il y a {since:.0f} s) — repère ┃ sur la jauge",
                "dim",
            )
        )
    age = device.age(now)
    lines.append(
        Line(
            f" Dernière trame il y a {age:.1f} s · {device.count} trames · "
            f"{device.rate(now):.1f}/s",
            "alert" if age > 5 else "dim",
        )
    )
    lines.append(Line(""))

    if device.marks:
        strongest = max(device.marks, key=lambda mark: mark.rssi)
        lines.append(Line(" Points relevés", "dim"))
        chunk = []
        for mark in device.marks[-8:]:
            flag = " ←" if mark is strongest else "  "
            chunk.append(f"{mark.index}) {mark.rssi:.0f} dBm{flag}")
        lines.append(Line("   " + "   ".join(chunk), "value"))
        lines.append(
            Line(
                "   Faute de direction mesurable, le point le plus fort "
                "indique la zone.",
                "dim",
            )
        )
        lines.append(Line(""))

    if samples_per_device is not None and 0 < samples_per_device <= 1.2:
        lines.append(
            Line(
                " ⚠ Une seule trame par appareil : votre plateforme déduplique "
                "les annonces.",
                "alert",
            )
        )
        lines.append(
            Line(
                "   Le suivi temps réel ne peut pas fonctionner ainsi. "
                "Relancez avec --restart-scan 2",
                "alert",
            )
        )
        lines.append(Line(""))

    lines.append(
        Line(
            " [m] relever un point   [r] recalibrer   [tab] suivant   [q] liste",
            "dim",
        )
    )
    return lines


def render_picker(
    state: LiveState,
    now: float,
    selected: int = 0,
    width: int = 80,
    trackers_only: bool = False,
) -> list[Line]:
    """Liste des appareils entendus, pour choisir une cible."""
    inner = max(40, width - 2)
    devices = state.ranked(now, trackers_only)
    known = sum(1 for device in state.devices.values() if device.signature is not None)

    lines = [
        Line(
            f" DÉTECTEUR — écoute en cours · {len(state.devices)} appareils · "
            f"{known} traceurs identifiés",
            "title",
        ),
        Line(
            " Choisissez une cible pour lancer la traque"
            + ("   [filtre : traceurs connus]" if trackers_only else ""),
            "dim",
        ),
        Line(" " + "─" * (inner - 1), "dim"),
    ]

    if not devices:
        lines.append(Line(""))
        lines.append(Line("     Aucun appareil entendu pour l'instant…", "dim"))
        lines.append(Line("     Le scan démarre, patientez quelques secondes.", "dim"))
        lines.append(Line(""))
        lines.append(Line(" [f] filtre traceurs   [q] quitter", "dim"))
        return lines

    lines.append(Line("    dBm  signal        appareil", "dim"))
    for index, device in enumerate(devices[:14]):
        smoothed = device.smoothed(now)
        stale = device.is_stale(now)
        bar = gauge(gauge_ratio(smoothed), 10) if smoothed is not None else " " * 10
        value = f"{smoothed:5.0f}" if smoothed is not None else "    —"
        name = device.label[: max(10, inner - 40)]
        flag = "⚑" if device.signature is not None else " "
        age = f"{device.age(now):4.0f}s"
        marker = "▸" if index == selected else " "
        lines.append(
            Line(
                f" {marker} {value}  {bar}  {flag} {name:<{max(10, inner - 40)}} {age}",
                "selected" if index == selected else ("dim" if stale else ""),
            )
        )

    lines.append(Line(""))
    lines.append(
        Line(" [↑↓] choisir   [entrée] traquer   [f] filtre   [q] quitter", "dim")
    )
    return lines
