"""Mise en forme des résultats : texte pour le terminal, JSON et HTML.

Le rapport doit rester lisible par quelqu'un qui n'a pas écrit le
détecteur : chaque score est accompagné du détail qui l'a produit, parce
qu'une accusation de filature se justifie, elle ne s'assène pas.
"""

from __future__ import annotations

import html
import json
import sys
import time
from typing import Sequence

from .geo import path_length
from .gnss_monitor import GnssVerdict
from .models import BleObservation, ThreatLevel, Verdict

LEVEL_MARKS = {
    ThreatLevel.NONE: "  ",
    ThreatLevel.LOW: " ·",
    ThreatLevel.SUSPECT: " ?",
    ThreatLevel.LIKELY: " !",
    ThreatLevel.CONFIRMED: "!!",
}

LEVEL_COLORS = {
    ThreatLevel.NONE: "\033[90m",
    ThreatLevel.LOW: "\033[37m",
    ThreatLevel.SUSPECT: "\033[33m",
    ThreatLevel.LIKELY: "\033[91m",
    ThreatLevel.CONFIRMED: "\033[1;31m",
}
RESET = "\033[0m"
WIDTH = 78


def _timestamp(value: float) -> str:
    return time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(value))


def _bar(ratio: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(1.0, ratio)) * width))
    return "█" * filled + "░" * (width - filled)


def observer_path(observations: Sequence[BleObservation]) -> list[tuple[float, float]]:
    """Reconstitue notre propre trajet à partir des observations horodatées."""
    seen: dict[float, tuple[float, float]] = {}
    for observation in sorted(observations, key=lambda obs: obs.timestamp):
        position = observation.position
        if position is not None:
            seen.setdefault(observation.timestamp, position)
    return [seen[key] for key in sorted(seen)]


def summarise(
    verdicts: Sequence[Verdict],
    observations: Sequence[BleObservation] = (),
    scan_times: Sequence[float] = (),
) -> dict:
    """Chiffres de synthèse d'une session de détection."""
    path = observer_path(observations)
    timestamps = [obs.timestamp for obs in observations] or list(scan_times)
    alerts = [verdict for verdict in verdicts if verdict.level >= ThreatLevel.SUSPECT]
    return {
        "emetteurs": len(verdicts),
        "observations": len(observations),
        "scans": len(scan_times),
        "debut": min(timestamps) if timestamps else None,
        "fin": max(timestamps) if timestamps else None,
        "distance_km": path_length(path) / 1000 if path else 0.0,
        "alertes": len(alerts),
        "niveau_max": max((v.level for v in verdicts), default=ThreatLevel.NONE).label,
    }


def render_text(
    verdicts: Sequence[Verdict],
    observations: Sequence[BleObservation] = (),
    scan_times: Sequence[float] = (),
    gnss: GnssVerdict | None = None,
    rf_lines: Sequence[str] = (),
    color: bool | None = None,
    limit: int | None = None,
    show_all: bool = False,
) -> str:
    """Rapport texte pour le terminal."""
    if color is None:
        color = sys.stdout.isatty()

    def paint(text: str, level: ThreatLevel) -> str:
        return f"{LEVEL_COLORS[level]}{text}{RESET}" if color else text

    stats = summarise(verdicts, observations, scan_times)
    lines = [
        "═" * WIDTH,
        "  DÉTECTEUR DE TRACEURS GPS — rapport du " + time.strftime("%d/%m/%Y %H:%M"),
        "═" * WIDTH,
    ]

    if stats["debut"] is not None:
        duration_min = (stats["fin"] - stats["debut"]) / 60
        lines.append(
            f"Période      : {_timestamp(stats['debut'])} → "
            f"{_timestamp(stats['fin'])} ({duration_min:.0f} min)"
        )
    lines.append(
        f"Couverture   : {stats['scans']} scans · {stats['observations']} trames · "
        f"{stats['distance_km']:.1f} km parcourus"
    )
    lines.append(
        f"Résultat     : {stats['emetteurs']} émetteurs, {stats['alertes']} à surveiller "
        f"(niveau max : {stats['niveau_max']})"
    )
    lines.append("")

    shown = [
        verdict
        for verdict in verdicts
        if show_all or verdict.level >= ThreatLevel.LOW or verdict.score > 0
    ]
    if limit is not None:
        shown = shown[:limit]

    if not shown:
        lines.append("Aucun émetteur retenu. Rien à signaler.")
        return "\n".join(lines)

    for verdict in shown:
        track = verdict.track
        header = (
            f"[{LEVEL_MARKS[verdict.level]}] {verdict.score:5.1f}/100  "
            f"{verdict.level.label.upper():9s} {verdict.label}"
        )
        lines.append(paint(header, verdict.level))
        lines.append(
            f"      {len(track.addresses)} adresse(s) · {len(track.observations)} "
            f"détections · {track.duration_s / 60:.0f} min · vu de "
            f"{_timestamp(track.first_seen)} à {_timestamp(track.last_seen)}"
        )
        lines.append(f"      MAC : {', '.join(track.addresses[:4])}")
        for criterion in verdict.criteria:
            lines.append(
                f"      {criterion.name:<16s} {_bar(criterion.ratio)} "
                f"{criterion.score:5.1f}/{criterion.weight:<5.1f} {criterion.detail}"
            )
        for note in verdict.notes:
            lines.append(f"      → {note}")
        lines.append("")

    top = shown[0]
    if top.level >= ThreatLevel.SUSPECT:
        lines.append("─" * WIDTH)
        lines.append(f"CONDUITE À TENIR ({top.label})")
        for recommendation in top.recommendations:
            lines.append(f"  • {recommendation}")
        lines.append("")

    if gnss is not None:
        lines.append("─" * WIDTH)
        lines.append(f"SIGNAL GNSS : {gnss.status.label}")
        for reason in gnss.reasons:
            lines.append(f"  • {reason}")
        lines.append("")

    if rf_lines:
        lines.append("─" * WIDTH)
        lines.append("BALAYAGE RF")
        lines.extend(f"  {line}" for line in rf_lines)
        lines.append("")

    return "\n".join(lines)


def render_json(
    verdicts: Sequence[Verdict],
    observations: Sequence[BleObservation] = (),
    scan_times: Sequence[float] = (),
    gnss: GnssVerdict | None = None,
) -> str:
    """Export JSON, destiné à l'archivage ou à une plainte."""
    payload = {
        "genere_le": time.time(),
        "resume": summarise(verdicts, observations, scan_times),
        "emetteurs": [verdict.to_dict() for verdict in verdicts],
    }
    if gnss is not None:
        payload["gnss"] = gnss.to_dict()
    return json.dumps(payload, indent=2, ensure_ascii=False)


_HTML_STYLE = """
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1a1a1a; --muted:#5c5c5c;
        --card:#f6f6f7; --line:#e0e0e2; --bar:#d8d8dc; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151517; --fg:#ededf0; --muted:#9a9aa2; --card:#1e1e22;
          --line:#2c2c31; --bar:#33333a; }
}
body { background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,sans-serif;
       margin:0; padding:2rem 1rem; }
main { max-width:60rem; margin:0 auto; }
h1 { font-size:1.4rem; margin:0 0 .25rem; }
.sub { color:var(--muted); margin-bottom:1.5rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:1rem 1.25rem; margin-bottom:1rem; }
.head { display:flex; align-items:baseline; gap:.75rem; flex-wrap:wrap; }
.score { font-size:1.5rem; font-weight:700; }
.badge { border-radius:999px; padding:.15rem .6rem; font-size:.75rem;
         text-transform:uppercase; letter-spacing:.04em; color:#fff; }
.aucune{background:#6b7280}.faible{background:#0e7490}.suspect{background:#b45309}
.probable{background:#c2410c}.confirmé{background:#b91c1c}
.meta { color:var(--muted); font-size:.85rem; margin:.35rem 0 .75rem; }
table { width:100%; border-collapse:collapse; font-size:.88rem; }
td { padding:.3rem .5rem .3rem 0; vertical-align:middle; }
td.name { width:9rem; } td.bar { width:8rem; }
.track { background:var(--bar); border-radius:4px; height:8px; overflow:hidden; }
.fill { background:currentColor; height:100%; }
.note { color:var(--muted); font-size:.85rem; margin-top:.5rem; }
ul.reco { margin:.5rem 0 0; padding-left:1.1rem; }
.wrap { overflow-x:auto; }
"""


def render_html(
    verdicts: Sequence[Verdict],
    observations: Sequence[BleObservation] = (),
    scan_times: Sequence[float] = (),
    gnss: GnssVerdict | None = None,
) -> str:
    """Rapport HTML autonome (aucune ressource externe)."""
    stats = summarise(verdicts, observations, scan_times)
    parts = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Rapport de détection de traceurs</title>",
        f"<style>{_HTML_STYLE}</style></head><body><main>",
        "<h1>Détection de traceurs GPS</h1>",
        f"<p class='sub'>{stats['scans']} scans · {stats['observations']} trames · "
        f"{stats['distance_km']:.1f} km parcourus · {stats['emetteurs']} émetteurs · "
        f"niveau maximal : {html.escape(stats['niveau_max'])}</p>",
    ]

    for verdict in verdicts:
        track = verdict.track
        parts.append("<section class='card'>")
        parts.append(
            f"<div class='head'><span class='score'>{verdict.score:.0f}</span>"
            f"<span class='badge {html.escape(verdict.level.label)}'>"
            f"{html.escape(verdict.level.label)}</span>"
            f"<strong>{html.escape(verdict.label)}</strong></div>"
        )
        parts.append(
            f"<div class='meta'>{len(track.addresses)} adresse(s) · "
            f"{len(track.observations)} détections · {track.duration_s / 60:.0f} min · "
            f"{html.escape(', '.join(track.addresses[:4]))}</div>"
        )
        parts.append("<div class='wrap'><table>")
        for criterion in verdict.criteria:
            parts.append(
                "<tr>"
                f"<td class='name'>{html.escape(criterion.name)}</td>"
                f"<td class='bar'><div class='track'><div class='fill' "
                f"style='width:{criterion.ratio * 100:.0f}%'></div></div></td>"
                f"<td>{criterion.score:.1f}/{criterion.weight:.0f}</td>"
                f"<td>{html.escape(criterion.detail)}</td>"
                "</tr>"
            )
        parts.append("</table></div>")
        for note in verdict.notes:
            parts.append(f"<p class='note'>{html.escape(note)}</p>")
        if verdict.level >= ThreatLevel.SUSPECT:
            parts.append("<ul class='reco'>")
            parts.extend(
                f"<li>{html.escape(item)}</li>" for item in verdict.recommendations
            )
            parts.append("</ul>")
        parts.append("</section>")

    if gnss is not None:
        parts.append(
            f"<section class='card'><strong>Signal GNSS : "
            f"{html.escape(gnss.status.label)}</strong><ul class='reco'>"
        )
        parts.extend(f"<li>{html.escape(reason)}</li>" for reason in gnss.reasons)
        parts.append("</ul></section>")

    parts.append("</main></body></html>")
    return "".join(parts)
