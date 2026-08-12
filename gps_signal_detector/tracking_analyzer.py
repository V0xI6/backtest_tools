"""Moteur de décision : « cet émetteur me suit-il ? »

Le principe est simple : un traceur posé sur vous se distingue d'un objet
connecté quelconque parce qu'il reste présent alors que *vous* vous
déplacez. Un téléviseur du voisinage est vu longtemps mais toujours au même
endroit ; une enceinte croisée en ville est vue partout mais brièvement. Le
traceur, lui, cumule durée, distance parcourue et lieux distincts.

Chaque critère est noté séparément et le score final est la somme
pondérée : le verdict reste explicable, ce qui compte quand la conclusion
peut mener à un dépôt de plainte.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .geo import cluster_positions, haversine, path_length
from .models import (
    BleObservation,
    Criterion,
    DeviceTrack,
    SafeZone,
    ThreatLevel,
    Verdict,
)
from .signatures import identify_track


@dataclass(frozen=True)
class Weights:
    """Poids des critères. Leur somme fait 100."""

    signature: float = 25.0
    separated: float = 10.0
    duration: float = 15.0
    zones: float = 25.0
    travel: float = 15.0
    proximity: float = 5.0
    presence: float = 5.0

    @property
    def total(self) -> float:
        return (
            self.signature
            + self.separated
            + self.duration
            + self.zones
            + self.travel
            + self.proximity
            + self.presence
        )


@dataclass(frozen=True)
class DetectionConfig:
    """Seuils de détection. Les valeurs par défaut visent un trajet routier."""

    weights: Weights = field(default_factory=Weights)

    min_observations: int = 3
    min_duration_s: float = 300.0  # en deçà de 5 min, on ne conclut pas
    full_duration_s: float = 3600.0
    full_zones: int = 4
    zone_radius_m: float = 150.0
    min_travel_m: float = 800.0
    full_travel_m: float = 10_000.0
    gps_noise_floor_m: float = 15.0
    weak_rssi: int = -95
    strong_rssi: int = -65
    min_presence: float = 0.25

    # Recollage des adresses MAC tournantes.
    rotation_max_gap_s: float = 1200.0
    rotation_max_overlap_s: float = 60.0
    rotation_rssi_delta: float = 15.0
    rotation_max_jump_m: float = 300.0
    rotation_max_speed_kmh: float = 150.0
    link_unknown_devices: bool = False

    scan_cycle_tolerance_s: float = 5.0
    safe_zone_penalty: float = 0.35


# Les deux bits de poids fort de l'octet le plus significatif encodent le
# type d'adresse aléatoire BLE. Une adresse publique peut prendre n'importe
# quelle valeur : ce n'est donc qu'une présomption.
_ADDRESS_TYPES = {
    0b11: "aléatoire statique",
    0b01: "privée résoluble",
    0b00: "privée non résoluble",
}


def guess_address_type(address: str) -> str:
    """Devine le type d'adresse BLE à partir de ses bits de poids fort."""
    try:
        first_octet = int(address.split(":")[0], 16)
    except (ValueError, IndexError):
        return "inconnue"
    return _ADDRESS_TYPES.get(first_octet >> 6, "publique probable")


def _ramp(value: float, low: float, high: float) -> float:
    """Rampe linéaire bornée entre 0 et 1."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def build_tracks(observations: Iterable[BleObservation]) -> list[DeviceTrack]:
    """Regroupe les observations par adresse BLE."""
    grouped: dict[str, list[BleObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.address, []).append(observation)

    tracks = []
    for address, items in grouped.items():
        identity = identify_track(items)
        tracks.append(
            DeviceTrack(
                key=address,
                observations=sorted(items, key=lambda obs: obs.timestamp),
                addresses=[address],
                signature=identity.signature,
                separated=identity.separated,
            )
        )
    return sorted(tracks, key=lambda track: track.first_seen)


def _rotates(track: DeviceTrack) -> bool:
    if track.signature is not None and track.signature.rotates_address:
        return True
    guessed = guess_address_type(track.addresses[0])
    return guessed in ("privée résoluble", "privée non résoluble")


def _signature_key(track: DeviceTrack) -> str | None:
    return track.signature.key if track.signature is not None else None


def can_link(a: DeviceTrack, b: DeviceTrack, config: DetectionConfig) -> bool:
    """Dit si `b` peut être la suite de `a` après rotation d'adresse MAC.

    Toutes les conditions doivent tenir : même famille de traceur, pas de
    chevauchement temporel réel (un même émetteur ne peut pas porter deux
    adresses en même temps), rotation assez rapprochée, puissance reçue
    comparable et positions compatibles.

    La tolérance de position n'est pas une constante : les deux points
    comparés sont *nos* positions successives. Si le traceur est sur nous,
    l'écart admissible est celui que nous avons pu parcourir pendant la
    rotation — sans cette marge, tout recollage échouerait en voiture.
    """
    if a.key == b.key:
        return False

    key_a, key_b = _signature_key(a), _signature_key(b)
    if key_a != key_b:
        return False
    if key_a is None and not config.link_unknown_devices:
        return False

    # `b` doit commencer après `a`, à un léger recouvrement près.
    if b.first_seen < a.last_seen - config.rotation_max_overlap_s:
        return False
    gap = b.first_seen - a.last_seen
    if gap > config.rotation_max_gap_s:
        return False

    median_a = statistics.median(a.rssi_values)
    median_b = statistics.median(b.rssi_values)
    if abs(median_a - median_b) > config.rotation_rssi_delta:
        return False

    positions_a, positions_b = a.positions, b.positions
    if positions_a and positions_b:
        reachable = config.rotation_max_jump_m + config.rotation_max_speed_kmh / 3.6 * max(
            gap, 0.0
        )
        if haversine(positions_a[-1], positions_b[0]) > reachable:
            return False
    return True


def link_identities(
    tracks: Sequence[DeviceTrack], config: DetectionConfig | None = None
) -> list[DeviceTrack]:
    """Recolle les pistes d'un même traceur qui fait tourner son adresse.

    Les AirTag et balises FMDN changent d'adresse toutes les quinze minutes
    environ. Sans ce recollage, un traceur suivi deux heures apparaîtrait
    comme huit appareils anodins vus quinze minutes chacun.
    """
    config = config or DetectionConfig()
    rotating = sorted(
        (track for track in tracks if _rotates(track)), key=lambda track: track.first_seen
    )
    static = [track for track in tracks if not _rotates(track)]

    consumed: set[str] = set()
    chains: list[DeviceTrack] = []
    for track in rotating:
        if track.key in consumed:
            continue
        consumed.add(track.key)
        chain = track
        extended = True
        while extended:
            extended = False
            for candidate in rotating:
                if candidate.key in consumed:
                    continue
                if can_link(chain, candidate, config):
                    chain = chain.merge(candidate)
                    consumed.add(candidate.key)
                    extended = True
                    break
        chains.append(chain)
    return sorted(chains + static, key=lambda track: track.first_seen)


def infer_scan_cycles(
    observations: Iterable[BleObservation], tolerance_s: float = 5.0
) -> list[float]:
    """Reconstitue les instants de scan à partir des observations.

    Sert à mesurer le taux de présence : un traceur embarqué répond à
    presque tous les scans, un appareil croisé au hasard à quelques-uns.
    """
    timestamps = sorted(observation.timestamp for observation in observations)
    cycles: list[float] = []
    for timestamp in timestamps:
        if not cycles or timestamp - cycles[-1] > tolerance_s:
            cycles.append(timestamp)
    return cycles


def _presence_rate(
    track: DeviceTrack, cycles: Sequence[float], tolerance_s: float
) -> tuple[float, int, int]:
    """Fraction des scans, sur la durée de vie de la piste, où elle répond."""
    if not cycles:
        return 0.0, 0, 0
    window = [
        cycle
        for cycle in cycles
        if track.first_seen - tolerance_s <= cycle <= track.last_seen + tolerance_s
    ]
    if not window:
        return 0.0, 0, 0
    seen = set()
    for observation in track.observations:
        closest = min(window, key=lambda cycle: abs(cycle - observation.timestamp))
        if abs(closest - observation.timestamp) <= tolerance_s:
            seen.add(closest)
    return len(seen) / len(window), len(seen), len(window)


def _in_safe_zone(position: tuple[float, float], safe_zones: Sequence[SafeZone]) -> bool:
    return any(
        haversine(position, zone.position) <= zone.radius_m for zone in safe_zones
    )


def _recommendations(level: ThreatLevel, track: DeviceTrack) -> list[str]:
    """Conduite à tenir, calibrée sur le niveau de menace."""
    if level <= ThreatLevel.LOW:
        return [
            "Rien d'anormal à ce stade. Laissez la détection tourner sur "
            "plusieurs trajets : un traceur ne se révèle qu'avec la distance."
        ]

    advice = [
        "Refaites un trajet de contrôle par un itinéraire inhabituel : "
        "un vrai traceur vous suivra, un appareil du décor non.",
        "Inspectez les emplacements classiques : pare-chocs, passages de "
        "roue, sous les sièges, coffre, plaque d'attelage, boîtier OBD.",
    ]
    if level >= ThreatLevel.LIKELY:
        advice = [
            "Ne jetez pas et n'abîmez pas l'appareil : c'est une preuve.",
            "Exportez le rapport horodaté (option --json) et conservez-le.",
            "Mettez-vous en lieu sûr avant d'inspecter le véhicule ou vos affaires.",
            "Déposez plainte : en France, poser un traceur sur autrui relève "
            "de l'atteinte à la vie privée (art. 226-1 du code pénal).",
            "Un traceur câblé sur la batterie doit être retiré par un "
            "professionnel, pas arraché.",
        ] + advice
    if track.signature is not None and track.signature.vendor == "Apple":
        advice.append(
            "Objet du réseau Find My : un iPhone, ou l'application Tracker "
            "Detect sur Android, peut le faire sonner pour le localiser."
        )
    return advice


class TrackingAnalyzer:
    """Analyse un flux d'observations BLE et en tire des verdicts."""

    def __init__(self, config: DetectionConfig | None = None) -> None:
        self.config = config or DetectionConfig()

    def analyze(
        self,
        observations: Iterable[BleObservation],
        scan_times: Sequence[float] | None = None,
        safe_zones: Sequence[SafeZone] = (),
        trusted: Iterable[str] = (),
    ) -> list[Verdict]:
        """Renvoie un verdict par émetteur, du plus inquiétant au moins."""
        observations = list(observations)
        if not observations:
            return []

        cycles = list(
            scan_times
            if scan_times is not None
            else infer_scan_cycles(observations, self.config.scan_cycle_tolerance_s)
        )
        tracks = link_identities(build_tracks(observations), self.config)
        trusted_set = {address.upper() for address in trusted}

        verdicts = [
            self._verdict(track, cycles, safe_zones, trusted_set) for track in tracks
        ]
        return sorted(verdicts, key=lambda verdict: verdict.score, reverse=True)

    def _verdict(
        self,
        track: DeviceTrack,
        cycles: Sequence[float],
        safe_zones: Sequence[SafeZone],
        trusted: set[str],
    ) -> Verdict:
        config = self.config
        weights = config.weights
        criteria: list[Criterion] = []
        notes: list[str] = []

        # Critère 1 : famille de traceur reconnue.
        if track.signature is not None:
            signature_score = weights.signature * track.signature.confidence_factor
            detail = (
                f"{track.signature.name} ({track.signature.vendor}), "
                f"confiance {track.signature.confidence}"
            )
        else:
            signature_score = 0.0
            detail = "aucune signature de traceur connue"
        criteria.append(Criterion("Signature", signature_score, weights.signature, detail))

        # Critère 2 : traceur séparé de son propriétaire.
        separated_score = weights.separated if track.separated else 0.0
        criteria.append(
            Criterion(
                "Mode séparé",
                separated_score,
                weights.separated,
                "émet en mode « séparé du propriétaire »"
                if track.separated
                else "non applicable",
            )
        )

        # Critère 3 : durée de présence.
        duration = track.duration_s
        duration_score = weights.duration * _ramp(
            duration, config.min_duration_s, config.full_duration_s
        )
        criteria.append(
            Criterion(
                "Durée",
                duration_score,
                weights.duration,
                f"présent {duration / 60:.0f} min sur {len(track.observations)} détections",
            )
        )

        positions = track.positions
        if positions:
            # Les critères spatiaux sont conditionnés par la durée : en
            # roulant, une balise de bord de route croisée trente secondes
            # traverse plusieurs « zones » sans rien suivre du tout. Seule
            # une présence installée dans le temps rend l'espace parlant.
            gate = _ramp(duration, config.min_duration_s / 2, config.min_duration_s)
            fleeting = " — atténué, présence trop brève pour conclure" if gate < 1 else ""

            zones = cluster_positions(positions, config.zone_radius_m)
            zone_score = weights.zones * _ramp(len(zones), 1, config.full_zones) * gate
            zone_detail = f"vu dans {len(zones)} zone(s) distincte(s){fleeting}"

            travel = path_length(positions, config.gps_noise_floor_m)
            travel_score = (
                weights.travel
                * _ramp(travel, config.min_travel_m, config.full_travel_m)
                * gate
            )
            travel_detail = f"{travel / 1000:.2f} km parcourus en sa présence{fleeting}"
        else:
            zone_score = travel_score = 0.0
            zone_detail = travel_detail = "aucune position GPS enregistrée"
            notes.append(
                "Analyse dégradée : sans position GPS, les deux critères les "
                "plus discriminants (zones et distance) sont inexploitables."
            )
        criteria.append(Criterion("Zones distinctes", zone_score, weights.zones, zone_detail))
        criteria.append(Criterion("Distance", travel_score, weights.travel, travel_detail))

        # Critère 6 : proximité, via la puissance reçue.
        median_rssi = statistics.median(track.rssi_values)
        proximity_score = weights.proximity * _ramp(
            median_rssi, config.weak_rssi, config.strong_rssi
        )
        criteria.append(
            Criterion(
                "Proximité",
                proximity_score,
                weights.proximity,
                f"RSSI médian {median_rssi:.0f} dBm (max {max(track.rssi_values)} dBm)",
            )
        )

        # Critère 7 : régularité de la présence d'un scan à l'autre.
        rate, seen, total = _presence_rate(track, cycles, config.scan_cycle_tolerance_s)
        presence_score = weights.presence * _ramp(rate, config.min_presence, 1.0)
        criteria.append(
            Criterion(
                "Régularité",
                presence_score,
                weights.presence,
                f"répond à {seen}/{total} scans ({rate * 100:.0f} %)",
            )
        )

        score = sum(criterion.score for criterion in criteria)

        if len(track.addresses) > 1:
            notes.append(
                f"{len(track.addresses)} adresses MAC recollées en un seul "
                "émetteur (rotation d'identifiant)."
            )

        # Garde-fous : on préfère taire un doute que crier au loup.
        if len(track.observations) < config.min_observations:
            score = min(score, ThreatLevel.LOW.threshold - 1)
            notes.append(
                f"Moins de {config.min_observations} détections : trop peu "
                "d'éléments pour conclure."
            )

        if positions and safe_zones and all(
            _in_safe_zone(position, safe_zones) for position in positions
        ):
            score *= config.safe_zone_penalty
            notes.append(
                "Émetteur jamais vu hors de vos zones de confiance : "
                "très probablement un appareil du voisinage."
            )

        if trusted & set(track.addresses):
            score = 0.0
            notes.append("Appareil déclaré de confiance.")

        score = max(0.0, min(100.0, score))
        level = ThreatLevel.from_score(score)
        return Verdict(
            track=track,
            score=score,
            level=level,
            criteria=criteria,
            notes=notes,
            recommendations=_recommendations(level, track),
        )
