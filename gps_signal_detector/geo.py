"""Petits utilitaires géographiques (stdlib uniquement)."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

Position = tuple[float, float]


def haversine(a: Position, b: Position) -> float:
    """Distance en mètres entre deux couples (latitude, longitude)."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def path_length(points: Sequence[Position], min_step_m: float = 15.0) -> float:
    """Longueur du trajet parcouru, en ignorant le bruit GPS.

    Les récepteurs grand public « respirent » de quelques mètres à l'arrêt.
    Sommer tous les écarts gonflerait artificiellement la distance d'un
    appareil immobile, donc les pas plus courts que `min_step_m` sont
    ignorés (l'ancre reste le dernier point retenu).
    """
    total = 0.0
    anchor: Position | None = None
    for point in points:
        if anchor is None:
            anchor = point
            continue
        step = haversine(anchor, point)
        if step >= min_step_m:
            total += step
            anchor = point
    return total


def cluster_positions(points: Iterable[Position], radius_m: float = 150.0) -> list[Position]:
    """Regroupe des positions en zones distinctes et renvoie leurs centres.

    Regroupement glouton par distance plutôt que par grille : une grille
    couperait en deux un même lieu situé à cheval sur une frontière de
    cellule, ce qui fausserait le comptage de zones.
    """
    centers: list[Position] = []
    counts: list[int] = []
    for point in points:
        for index, center in enumerate(centers):
            if haversine(center, point) <= radius_m:
                count = counts[index]
                # Moyenne incrémentale : le centre suit le barycentre du groupe.
                centers[index] = (
                    (center[0] * count + point[0]) / (count + 1),
                    (center[1] * count + point[1]) / (count + 1),
                )
                counts[index] = count + 1
                break
        else:
            centers.append(point)
            counts.append(1)
    return centers


def max_spread(points: Sequence[Position]) -> float:
    """Plus grande distance entre deux positions de la liste."""
    if len(points) < 2:
        return 0.0
    best = 0.0
    for i in range(len(points) - 1):
        for j in range(i + 1, len(points)):
            best = max(best, haversine(points[i], points[j]))
    return best


def offset(origin: Position, north_m: float, east_m: float) -> Position:
    """Décale une position de N mètres vers le nord et E mètres vers l'est."""
    lat = origin[0] + north_m / 111_320.0
    denominator = 111_320.0 * math.cos(math.radians(origin[0]))
    lon = origin[1] + (east_m / denominator if abs(denominator) > 1e-9 else 0.0)
    return (lat, lon)


def interpolate(a: Position, b: Position, steps: int) -> list[Position]:
    """Découpe le segment a→b en `steps` positions intermédiaires."""
    if steps <= 1:
        return [b]
    return [
        (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
        for i in range(1, steps + 1)
    ]
