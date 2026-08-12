"""Surveillance de la qualité du signal GNSS (brouillage et leurre).

Deux usages complémentaires de la détection de traceurs :

* un brouilleur GPS embarqué dans un véhicule voisin fait chuter le rapport
  signal/bruit de tous les satellites d'un coup ;
* un leurre (spoofing) fabrique des signaux trop parfaits : puissances
  anormalement uniformes, précision impossible, sauts de position.

Le module ne lit que du NMEA 0183, ce que produit n'importe quel récepteur
GPS USB ou téléphone en mode debug. Aucune dépendance externe.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator

from .geo import haversine

KNOTS_TO_KMH = 1.852


class GnssStatus(Enum):
    """État de santé du signal GNSS."""

    OK = "nominal"
    DEGRADED = "dégradé"
    JAMMING = "brouillage probable"
    SPOOFING = "leurre probable"

    def __init__(self, label: str) -> None:
        self.label = label


@dataclass(frozen=True)
class GnssConfig:
    """Seuils de décision, exprimés dans les unités du NMEA."""

    jam_cn0_floor: float = 25.0  # dB-Hz : en dessous, aucune poursuite fiable
    jam_drop_db: float = 12.0  # chute brutale par rapport à la ligne de base
    min_sats_for_jam: int = 4
    spoof_cn0_stdev: float = 2.0  # un vrai ciel n'est jamais aussi régulier
    spoof_min_sats: int = 6
    # Un brouilleur écrase lui aussi toutes les porteuses de façon uniforme.
    # L'uniformité ne trahit un leurre que si les signaux restent *forts* :
    # en dessous de ce seuil, c'est un brouillage, pas une contrefaçon.
    spoof_min_cn0: float = 30.0
    spoof_max_speed_kmh: float = 900.0
    spoof_perfect_hdop: float = 0.4
    spoof_perfect_sats: int = 8
    baseline_window: int = 20


@dataclass
class GnssSnapshot:
    """Photographie de l'état du récepteur à un instant donné."""

    fix_quality: int = 0
    fix_type: int = 1  # 1 = aucun, 2 = 2D, 3 = 3D
    sats_used: int = 0
    hdop: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    speed_kmh: float | None = None
    timestamp: str | None = None
    satellites: dict[str, float] = field(default_factory=dict)  # PRN -> C/N0

    @property
    def has_fix(self) -> bool:
        return self.fix_quality > 0 or self.fix_type >= 2

    @property
    def position(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return (self.latitude, self.longitude)

    @property
    def cn0_values(self) -> list[float]:
        return [value for value in self.satellites.values() if value > 0]

    @property
    def mean_cn0(self) -> float:
        values = self.cn0_values
        return statistics.fmean(values) if values else 0.0


@dataclass
class GnssVerdict:
    status: GnssStatus
    reasons: list[str] = field(default_factory=list)
    snapshot: GnssSnapshot = field(default_factory=GnssSnapshot)

    def to_dict(self) -> dict:
        return {
            "statut": self.status.label,
            "raisons": list(self.reasons),
            "satellites_utilises": self.snapshot.sats_used,
            "satellites_visibles": len(self.snapshot.satellites),
            "cn0_moyen": round(self.snapshot.mean_cn0, 1),
            "hdop": self.snapshot.hdop,
            "fix": self.snapshot.has_fix,
        }


def _checksum(payload: str) -> int:
    result = 0
    for char in payload:
        result ^= ord(char)
    return result


def parse_sentence(line: str) -> tuple[str, list[str]] | None:
    """Valide et découpe une phrase NMEA.

    Renvoie (type sans identifiant de constellation, champs), ou None si la
    trame est malformée ou si sa somme de contrôle est fausse.
    """
    line = line.strip()
    if not line.startswith("$") or "," not in line:
        return None
    body = line[1:]
    if "*" in body:
        body, _, checksum_text = body.partition("*")
        try:
            if _checksum(body) != int(checksum_text[:2], 16):
                return None
        except ValueError:
            return None
    fields = body.split(",")
    talker = fields[0]
    if len(talker) < 5:
        return None
    return talker[2:], fields[1:]


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coordinate(value: str, hemisphere: str) -> float | None:
    """Convertit une coordonnée NMEA (ddmm.mmmm) en degrés décimaux."""
    raw = _to_float(value)
    if raw is None or not hemisphere:
        return None
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    decimal = degrees + minutes / 60.0
    return -decimal if hemisphere.upper() in ("S", "W") else decimal


class GnssMonitor:
    """Agrège les phrases NMEA et évalue la santé du signal."""

    def __init__(self, config: GnssConfig | None = None) -> None:
        self.config = config or GnssConfig()
        self.snapshot = GnssSnapshot()
        self._baseline: deque[float] = deque(maxlen=self.config.baseline_window)
        self._had_fix = False
        self._last_position: tuple[float, float] | None = None
        self._last_position_time: float | None = None
        self._gsv_accumulator: dict[str, float] = {}
        self._elapsed = 0.0

    def feed(self, line: str) -> bool:
        """Ingère une phrase NMEA. Renvoie True si elle a été exploitée."""
        parsed = parse_sentence(line)
        if parsed is None:
            return False
        kind, fields = parsed
        handler = {
            "GGA": self._handle_gga,
            "RMC": self._handle_rmc,
            "GSA": self._handle_gsa,
            "GSV": self._handle_gsv,
        }.get(kind)
        if handler is None:
            return False
        handler(fields)
        return True

    def feed_many(self, lines: Iterable[str]) -> int:
        return sum(1 for line in lines if self.feed(line))

    def _handle_gga(self, fields: list[str]) -> None:
        if len(fields) < 8:
            return
        self.snapshot.timestamp = fields[0] or self.snapshot.timestamp
        self.snapshot.latitude = _coordinate(fields[1], fields[2])
        self.snapshot.longitude = _coordinate(fields[3], fields[4])
        self.snapshot.fix_quality = _to_int(fields[5])
        self.snapshot.sats_used = _to_int(fields[6])
        self.snapshot.hdop = _to_float(fields[7])

    def _handle_rmc(self, fields: list[str]) -> None:
        if len(fields) < 7:
            return
        self.snapshot.timestamp = fields[0] or self.snapshot.timestamp
        if fields[1].upper() == "A":
            self.snapshot.latitude = _coordinate(fields[2], fields[3])
            self.snapshot.longitude = _coordinate(fields[4], fields[5])
        speed_knots = _to_float(fields[6])
        if speed_knots is not None:
            self.snapshot.speed_kmh = speed_knots * KNOTS_TO_KMH

    def _handle_gsa(self, fields: list[str]) -> None:
        if len(fields) < 2:
            return
        self.snapshot.fix_type = _to_int(fields[1], default=1)
        if len(fields) >= 16:
            hdop = _to_float(fields[15])
            if hdop is not None:
                self.snapshot.hdop = hdop

    def _handle_gsv(self, fields: list[str]) -> None:
        """Accumule les satellites en vue, répartis sur plusieurs phrases."""
        if len(fields) < 3:
            return
        total_messages = _to_int(fields[0], default=1)
        message_number = _to_int(fields[1], default=1)
        if message_number == 1:
            self._gsv_accumulator = {}

        for index in range(3, len(fields) - 3, 4):
            prn = fields[index]
            if not prn:
                continue
            snr = _to_float(fields[index + 3])
            self._gsv_accumulator[prn] = snr if snr is not None else 0.0

        if message_number >= total_messages:
            self.snapshot.satellites = dict(self._gsv_accumulator)

    def assess(self, elapsed_s: float = 1.0) -> GnssVerdict:
        """Évalue l'état courant et met à jour la ligne de base."""
        config = self.config
        snapshot = self.snapshot
        reasons: list[str] = []
        jamming = False
        spoofing = False

        visible = len(snapshot.satellites)
        cn0_values = snapshot.cn0_values
        mean_cn0 = snapshot.mean_cn0
        baseline = statistics.median(self._baseline) if self._baseline else None

        # --- Brouillage : les satellites restent visibles mais muets.
        if visible >= config.min_sats_for_jam and mean_cn0 and mean_cn0 < config.jam_cn0_floor:
            jamming = True
            reasons.append(
                f"C/N0 moyen à {mean_cn0:.1f} dB-Hz pour {visible} satellites "
                f"en vue (plancher {config.jam_cn0_floor:.0f})."
            )
        if baseline is not None and mean_cn0 and baseline - mean_cn0 >= config.jam_drop_db:
            jamming = True
            reasons.append(
                f"Chute de {baseline - mean_cn0:.1f} dB par rapport à la "
                f"ligne de base ({baseline:.1f} dB-Hz)."
            )
        if self._had_fix and not snapshot.has_fix and visible >= config.min_sats_for_jam:
            jamming = True
            reasons.append(
                "Perte du point fixe alors que les satellites restent visibles."
            )

        # --- Leurre : un signal trop propre pour être honnête.
        if len(cn0_values) >= config.spoof_min_sats and mean_cn0 >= config.spoof_min_cn0:
            spread = statistics.pstdev(cn0_values)
            if spread < config.spoof_cn0_stdev:
                spoofing = True
                reasons.append(
                    f"Puissances anormalement uniformes (écart-type "
                    f"{spread:.2f} dB sur {len(cn0_values)} satellites) à "
                    f"{mean_cn0:.0f} dB-Hz : un ciel réel varie avec l'élévation."
                )
        if (
            snapshot.hdop is not None
            and snapshot.hdop < config.spoof_perfect_hdop
            and snapshot.sats_used >= config.spoof_perfect_sats
        ):
            spoofing = True
            reasons.append(f"HDOP de {snapshot.hdop:.2f}, trop bon pour être vrai.")

        position = snapshot.position
        if position is not None and self._last_position is not None:
            delta_s = max(elapsed_s, 1e-3)
            speed = haversine(self._last_position, position) / delta_s * 3.6
            if speed > config.spoof_max_speed_kmh:
                spoofing = True
                reasons.append(
                    f"Saut de position impliquant {speed:.0f} km/h, "
                    "physiquement impossible."
                )

        # Priorité explicite plutôt que « dernière règle gagnante » : un
        # leurre est le diagnostic le plus grave et le plus spécifique.
        if spoofing:
            status = GnssStatus.SPOOFING
        elif jamming:
            status = GnssStatus.JAMMING
        elif not snapshot.has_fix:
            status = GnssStatus.DEGRADED
            reasons.append("Aucun point fixe (ciel masqué, tunnel, démarrage à froid).")
        elif snapshot.hdop is not None and snapshot.hdop > 5:
            status = GnssStatus.DEGRADED
            reasons.append(f"HDOP de {snapshot.hdop:.1f} : précision médiocre.")
        else:
            status = GnssStatus.OK

        if mean_cn0:
            self._baseline.append(mean_cn0)
        self._had_fix = self._had_fix or snapshot.has_fix
        if position is not None:
            self._last_position = position

        if not reasons:
            reasons.append(
                f"{snapshot.sats_used} satellites utilisés, C/N0 moyen "
                f"{mean_cn0:.1f} dB-Hz."
            )
        return GnssVerdict(status=status, reasons=reasons, snapshot=snapshot)


def read_nmea_file(path: str) -> Iterator[str]:
    """Lit un journal NMEA ligne à ligne (fichier enregistré ou tube)."""
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            yield line


def analyse_log(path: str, config: GnssConfig | None = None) -> GnssVerdict:
    """Rejoue un journal NMEA complet et renvoie le verdict final."""
    monitor = GnssMonitor(config)
    monitor.feed_many(read_nmea_file(path))
    return monitor.assess()
