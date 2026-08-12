"""État temps réel des émetteurs vus autour de nous.

Ce module tient la fenêtre glissante des mesures et en tire ce qui sert à
chercher physiquement un traceur : puissance lissée, tendance, bande de
proximité.

Un mot sur le lissage, parce que c'est ce qui rend l'outil utilisable. Le
RSSI brut saute de ±10 dB d'une trame à l'autre — réflexions sur les murs
ou la tôle, orientation de l'antenne, votre propre corps qui fait écran.
Affichée telle quelle, la jauge serait illisible. On prend donc la
**médiane** sur quelques secondes plutôt qu'une moyenne : elle encaisse les
valeurs aberrantes sans se laisser tirer par elles.

Et un avertissement qu'il faut garder en tête en lisant les distances : le
RSSI est un mauvais instrument en valeur absolue (l'erreur courante va de
50 à 100 %), mais un excellent instrument en **relatif**. « Je me
rapproche » est fiable ; « il est à 2,3 m » ne l'est pas.
"""

from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

from .models import BleObservation, TrackerSignature
from .signatures import identify

# Le lissage compte des échantillons, pas des secondes. Une fenêtre de temps
# fixe ne conviendrait pas : un AirTag n'émet que toutes les 2 secondes, une
# fenêtre de 3 s ne contiendrait qu'une mesure ou deux et ne lisserait rien.
# En raisonnant par nombre d'échantillons, la fenêtre se règle d'elle-même
# sur la cadence de chaque appareil — rapide et réactive pour un émetteur
# bavard, plus étalée pour un émetteur économe.
SMOOTHING_SAMPLES = 5
SMOOTHING_MAX_AGE_S = 12.0

HISTORY_WINDOW_S = 60.0
STALE_AFTER_S = 15.0
FORGET_AFTER_S = 300.0

# Bandes de proximité. Les distances sont des ordres de grandeur, pas des
# mesures : elles dépendent de la puissance d'émission de l'appareil, des
# obstacles et des réflexions.
PROXIMITY_BANDS: tuple[tuple[float, str, str], ...] = (
    (-50.0, "À PORTÉE DE MAIN", "moins d'un mètre"),
    (-65.0, "TRÈS PROCHE", "environ 1 à 3 mètres"),
    (-78.0, "PROCHE", "environ 3 à 10 mètres"),
    (-90.0, "À DISTANCE", "environ 10 à 30 mètres"),
)
FAR_BAND = ("LIMITE DE PORTÉE", "au-delà de 30 mètres")

# Bornes de l'échelle de signal. En dessous de -100 dBm on ne reçoit plus
# rien d'exploitable ; au-dessus de -35 le récepteur sature et les derniers
# centimètres cessent de se distinguer — une limite du RSSI, pas du code.
SIGNAL_FLOOR = -100.0
SIGNAL_CEILING = -35.0


def signal_ratio(rssi: float) -> float:
    """Ramène une puissance en dBm sur une échelle de 0 à 1."""
    span = SIGNAL_CEILING - SIGNAL_FLOOR
    return max(0.0, min(1.0, (rssi - SIGNAL_FLOOR) / span))

# Paliers de tendance, en dB sur la fenêtre de lissage.
TREND_LEVELS: tuple[tuple[float, str, str], ...] = (
    (3.0, "▲▲▲  VOUS CHAUFFEZ", "hot"),
    (1.0, "▲    ça monte", "warm"),
    (-1.0, "→    stable", "flat"),
    (-3.0, "▼    ça descend", "cool"),
)
TREND_FALLING = ("▼▼▼  vous refroidissez", "cold")


def bucketed_series(
    samples: Sequence[tuple[float, int]],
    now: float,
    window_s: float,
    buckets: int,
) -> list[float | None]:
    """Découpe l'historique en colonnes de temps égales.

    Les trames n'arrivent pas à cadence régulière : chaque colonne prend la
    médiane des mesures qu'elle contient. Les trous courts sont comblés par
    la dernière valeur connue — sans quoi un émetteur lent (un AirTag, deux
    secondes entre deux trames) donnerait une courbe en pointillé illisible.
    Un silence réel, lui, reste un trou : c'est une information.

    Partagé par l'affichage terminal et l'interface web, pour que les deux
    racontent exactement la même chose.
    """
    if buckets <= 0:
        return []

    grouped: list[list[int]] = [[] for _ in range(buckets)]
    times: list[float] = []
    start = now - window_s
    for timestamp, rssi in samples:
        if timestamp < start or timestamp > now:
            continue
        index = int((timestamp - start) / window_s * buckets)
        grouped[max(0, min(buckets - 1, index))].append(rssi)
        times.append(timestamp)

    values: list[float | None] = [
        statistics.median(bucket) if bucket else None for bucket in grouped
    ]
    if not any(value is not None for value in values):
        return values

    intervals = [
        later - earlier for earlier, later in zip(times, times[1:]) if later > earlier
    ]
    if intervals:
        typical = statistics.median(intervals)
        max_hold = max(1, math.ceil(3 * typical / window_s * buckets))
    else:
        max_hold = 1

    filled: list[float | None] = []
    held: float | None = None
    holding = 0
    for value in values:
        if value is not None:
            filled.append(value)
            held = value
            holding = 0
        elif held is not None and holding < max_hold:
            filled.append(held)
            holding += 1
        else:
            filled.append(None)
            held = None
    return filled


def proximity_band(rssi: float) -> tuple[str, str]:
    """Bande de proximité approximative pour une puissance donnée."""
    for threshold, label, hint in PROXIMITY_BANDS:
        if rssi >= threshold:
            return label, hint
    return FAR_BAND


def trend_label(delta: float | None) -> tuple[str, str]:
    """Libellé « chaud / froid » pour une variation en dB."""
    if delta is None:
        return "…    en attente de mesures", "flat"
    for threshold, label, style in TREND_LEVELS:
        if delta >= threshold:
            return label, style
    return TREND_FALLING


@dataclass
class Mark:
    """Point de mesure marqué à la main pendant une recherche."""

    index: int
    timestamp: float
    rssi: float
    label: str = ""


@dataclass
class LiveDevice:
    """Un émetteur et son historique récent."""

    address: str
    name: str | None = None
    signature: TrackerSignature | None = None
    separated: bool = False
    battery: str | None = None
    samples: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=4000))
    first_seen: float = 0.0
    last_seen: float = 0.0
    count: int = 0
    best_rssi: float | None = None
    best_at: float = 0.0
    marks: list[Mark] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.signature is not None:
            return self.signature.name
        return self.address

    def record(self, observation: BleObservation) -> None:
        """Enregistre une trame."""
        if not self.first_seen:
            self.first_seen = observation.timestamp
        self.last_seen = observation.timestamp
        self.count += 1
        self.samples.append((observation.timestamp, observation.rssi))

        if observation.name:
            self.name = observation.name
        identity = identify(observation)
        if identity.signature is not None and (
            self.signature is None
            or identity.signature.confidence_factor > self.signature.confidence_factor
        ):
            self.signature = identity.signature
        if identity.separated:
            self.separated = True
        if identity.battery:
            self.battery = identity.battery
        # Le meilleur signal se suit à la mesure, pas à l'affichage : les deux
        # interfaces doivent voir le même repère.
        self.update_best(observation.timestamp)

    def values_between(self, start: float, stop: float) -> list[int]:
        return [rssi for timestamp, rssi in self.samples if start <= timestamp <= stop]

    def recent_samples(
        self,
        now: float,
        count: int = SMOOTHING_SAMPLES,
        max_age_s: float = SMOOTHING_MAX_AGE_S,
    ) -> list[int]:
        """Les `count` dernières mesures, en écartant les trop vieilles."""
        values: list[int] = []
        for timestamp, rssi in reversed(self.samples):
            if now - timestamp > max_age_s:
                break
            values.append(rssi)
            if len(values) >= count:
                break
        return values

    def smoothed(
        self,
        now: float,
        count: int = SMOOTHING_SAMPLES,
        max_age_s: float = SMOOTHING_MAX_AGE_S,
    ) -> float | None:
        """Puissance lissée : médiane des dernières mesures."""
        values = self.recent_samples(now, count, max_age_s)
        if not values:
            return None
        return statistics.median(values)

    def trend(
        self,
        now: float,
        count: int = SMOOTHING_SAMPLES,
        max_age_s: float = SMOOTHING_MAX_AGE_S,
    ) -> float | None:
        """Variation en dB entre les dernières mesures et celles d'avant.

        C'est le cœur du jeu du chaud/froid : positif, vous vous rapprochez.
        On exige au moins deux mesures de chaque côté — sur une seule, on
        mesurerait le bruit et la jauge s'affolerait sans raison.
        """
        recent: list[int] = []
        older: list[int] = []
        for timestamp, rssi in reversed(self.samples):
            if now - timestamp > max_age_s * 2:
                break
            if len(recent) < count:
                recent.append(rssi)
            elif len(older) < count:
                older.append(rssi)
            else:
                break
        if len(recent) < 2 or len(older) < 2:
            return None
        return statistics.median(recent) - statistics.median(older)

    def rate(self, now: float, window_s: float = 10.0) -> float:
        """Trames reçues par seconde — révèle la cadence d'émission."""
        values = self.values_between(now - window_s, now)
        return len(values) / window_s if window_s > 0 else 0.0

    def age(self, now: float) -> float:
        # Borné à zéro : une trame peut être horodatée juste après l'instant
        # de rendu, et un « il y a -0,1 s » n'aurait aucun sens à l'écran.
        return max(0.0, now - self.last_seen)

    def is_stale(self, now: float, timeout_s: float = STALE_AFTER_S) -> bool:
        return self.age(now) > timeout_s

    def update_best(self, now: float) -> None:
        """Retient le meilleur signal de la session de recherche."""
        current = self.smoothed(now)
        if current is None:
            return
        if self.best_rssi is None or current > self.best_rssi:
            self.best_rssi = current
            self.best_at = now

    def reset_calibration(self) -> None:
        """Repart à zéro : utile quand on change de pièce ou de véhicule."""
        self.best_rssi = None
        self.best_at = 0.0
        self.marks.clear()

    def mark(self, now: float, label: str = "") -> Mark | None:
        """Note la puissance à l'endroit où l'on se trouve.

        Faute de direction mesurable, c'est ainsi qu'on localise : relever
        plusieurs points et garder le plus fort.
        """
        current = self.smoothed(now)
        if current is None:
            return None
        entry = Mark(index=len(self.marks) + 1, timestamp=now, rssi=current, label=label)
        self.marks.append(entry)
        return entry


class LiveState:
    """Ensemble des émetteurs entendus, tenu à jour en continu."""

    def __init__(self, forget_after_s: float = FORGET_AFTER_S) -> None:
        self.devices: dict[str, LiveDevice] = {}
        self.forget_after_s = forget_after_s
        self.total_observations = 0
        self.started_at: float | None = None
        # L'acquisition écrit depuis la boucle d'événements, l'interface web
        # lit depuis les fils du serveur HTTP : sans verrou, une insertion
        # pendant un parcours ferait tomber la page.
        self.lock = threading.RLock()

    def record(self, observation: BleObservation) -> LiveDevice:
        with self.lock:
            if self.started_at is None:
                self.started_at = observation.timestamp
            device = self.devices.get(observation.address)
            if device is None:
                device = LiveDevice(address=observation.address)
                self.devices[observation.address] = device
            device.record(observation)
            self.total_observations += 1
            return device

    def prune(self, now: float) -> int:
        """Oublie les appareils muets depuis trop longtemps."""
        with self.lock:
            expired = [
                address
                for address, device in self.devices.items()
                if device.age(now) > self.forget_after_s
            ]
            for address in expired:
                del self.devices[address]
            return len(expired)

    def ranked(self, now: float, trackers_only: bool = False) -> list[LiveDevice]:
        """Appareils triés du signal le plus fort au plus faible.

        Les appareils devenus muets basculent en fin de liste : ils ne sont
        plus exploitables pour une recherche en cours.
        """
        with self.lock:
            devices = list(self.devices.values())
        if trackers_only:
            devices = [device for device in devices if device.signature is not None]

        def sort_key(device: LiveDevice):
            smoothed = device.smoothed(now)
            return (
                device.is_stale(now),
                -(smoothed if smoothed is not None else -999.0),
            )

        return sorted(devices, key=sort_key)

    def samples_per_device(self, now: float, window_s: float = 5.0) -> float:
        """Nombre moyen de trames par appareil sur la fenêtre récente.

        Sert à repérer une plateforme qui déduplique les annonces : si ce
        chiffre reste à 1, le RSSI ne bougera jamais et la traque est
        inopérante. C'est le comportement par défaut de CoreBluetooth sur
        macOS quand l'option « AllowDuplicates » n'est pas active.
        """
        with self.lock:
            devices = list(self.devices.values())
        if not devices:
            return 0.0
        counts = [len(device.values_between(now - window_s, now)) for device in devices]
        active = [count for count in counts if count]
        return statistics.fmean(active) if active else 0.0
