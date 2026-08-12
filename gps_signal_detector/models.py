"""Structures de données du détecteur de traceurs GPS.

Ce module ne dépend que de la bibliothèque standard : il doit rester
importable sur une machine sans matériel ni dépendance optionnelle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering

# UUID de base Bluetooth SIG : sert à réduire un UUID 128 bits en 16 bits.
BLUETOOTH_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"


def short_uuid(uuid: str) -> str:
    """Réduit un UUID Bluetooth à sa forme courte 16 bits ("feed").

    Accepte les formes "0000feed-0000-1000-8000-00805f9b34fb", "0xFEED",
    "FEED" ou "feed". Les UUID propriétaires 128 bits sont renvoyés
    normalisés en minuscules, sans être tronqués.
    """
    value = uuid.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    if value.endswith(BLUETOOTH_BASE_UUID_SUFFIX) and len(value) == 36:
        return value[4:8]
    if len(value) == 4:
        return value
    return value


@dataclass(slots=True)
class BleObservation:
    """Une trame publicitaire BLE vue à un instant donné.

    `latitude` / `longitude` sont la position de l'observateur (nous), pas
    celle de l'émetteur : c'est ce déplacement qui permet de distinguer un
    traceur qui nous suit d'un appareil fixe croisé au passage.
    """

    timestamp: float
    address: str
    rssi: int = -100
    name: str | None = None
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_uuids: tuple[str, ...] = ()
    service_data: dict[str, bytes] = field(default_factory=dict)
    latitude: float | None = None
    longitude: float | None = None
    address_type: str | None = None  # "public", "random" ou None si inconnu

    def __post_init__(self) -> None:
        self.address = self.address.upper()
        self.service_uuids = tuple(short_uuid(u) for u in self.service_uuids)
        self.service_data = {short_uuid(k): v for k, v in self.service_data.items()}

    @property
    def position(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return (self.latitude, self.longitude)

    def to_dict(self) -> dict:
        """Forme sérialisable (les octets sont encodés en hexadécimal)."""
        return {
            "timestamp": self.timestamp,
            "address": self.address,
            "rssi": self.rssi,
            "name": self.name,
            "manufacturer_data": {
                str(cid): payload.hex() for cid, payload in self.manufacturer_data.items()
            },
            "service_uuids": list(self.service_uuids),
            "service_data": {uuid: payload.hex() for uuid, payload in self.service_data.items()},
            "latitude": self.latitude,
            "longitude": self.longitude,
            "address_type": self.address_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BleObservation":
        return cls(
            timestamp=float(data["timestamp"]),
            address=data["address"],
            rssi=int(data.get("rssi", -100)),
            name=data.get("name"),
            manufacturer_data={
                int(cid): bytes.fromhex(payload)
                for cid, payload in (data.get("manufacturer_data") or {}).items()
            },
            service_uuids=tuple(data.get("service_uuids") or ()),
            service_data={
                uuid: bytes.fromhex(payload)
                for uuid, payload in (data.get("service_data") or {}).items()
            },
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            address_type=data.get("address_type"),
        )


@dataclass(frozen=True)
class TrackerSignature:
    """Signature d'une famille de traceurs commerciaux.

    `confidence` traduit notre certitude sur les identifiants publics
    utilisés : une signature « moyenne » contribue moins au score final
    qu'une signature « haute ».
    """

    key: str
    name: str
    vendor: str
    company_ids: tuple[int, ...] = ()
    mfg_prefixes: tuple[tuple[int, bytes], ...] = ()
    service_uuids: tuple[str, ...] = ()
    service_data_prefixes: tuple[tuple[str, bytes], ...] = ()
    name_patterns: tuple[str, ...] = ()
    rotates_address: bool = True
    confidence: str = "haute"
    notes: str = ""

    @property
    def confidence_factor(self) -> float:
        return {"haute": 1.0, "moyenne": 0.7, "faible": 0.45}.get(self.confidence, 0.7)


@dataclass(frozen=True)
class SafeZone:
    """Lieu de confiance (domicile, bureau) où un émetteur fixe est normal."""

    label: str
    latitude: float
    longitude: float
    radius_m: float = 200.0

    @property
    def position(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


@total_ordering
class ThreatLevel(Enum):
    """Niveau de menace, du plus bas au plus haut."""

    NONE = ("aucune", 0.0)
    LOW = ("faible", 25.0)
    SUSPECT = ("suspect", 50.0)
    LIKELY = ("probable", 70.0)
    CONFIRMED = ("confirmé", 85.0)

    def __init__(self, label: str, threshold: float) -> None:
        self.label = label
        self.threshold = threshold

    def __lt__(self, other: "ThreatLevel") -> bool:
        if not isinstance(other, ThreatLevel):
            return NotImplemented
        return self.threshold < other.threshold

    @classmethod
    def from_score(cls, score: float) -> "ThreatLevel":
        level = cls.NONE
        for candidate in cls:
            if score >= candidate.threshold:
                level = candidate
        return level


@dataclass(slots=True)
class DeviceTrack:
    """Toutes les observations rattachées à un même émetteur présumé.

    Un traceur qui fait tourner son adresse MAC produit plusieurs pistes
    que `link_identities()` recolle ensuite en une seule.
    """

    key: str
    observations: list[BleObservation] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    signature: TrackerSignature | None = None
    separated: bool = False  # traceur séparé de son propriétaire (mode « perdu »)
    linked_from: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.observations.sort(key=lambda obs: obs.timestamp)
        if not self.addresses:
            self.addresses = sorted({obs.address for obs in self.observations})

    @property
    def first_seen(self) -> float:
        return self.observations[0].timestamp

    @property
    def last_seen(self) -> float:
        return self.observations[-1].timestamp

    @property
    def duration_s(self) -> float:
        return self.last_seen - self.first_seen

    @property
    def rssi_values(self) -> list[int]:
        return [obs.rssi for obs in self.observations]

    @property
    def positions(self) -> list[tuple[float, float]]:
        return [obs.position for obs in self.observations if obs.position is not None]

    @property
    def label(self) -> str:
        """Nom lisible : nom BLE annoncé, sinon famille, sinon adresse."""
        for obs in self.observations:
            if obs.name:
                return obs.name
        if self.signature is not None:
            return self.signature.name
        return self.addresses[0] if self.addresses else self.key

    def merge(self, other: "DeviceTrack") -> "DeviceTrack":
        """Fusionne deux pistes appartenant au même émetteur."""
        return DeviceTrack(
            key=self.key,
            observations=sorted(
                self.observations + other.observations, key=lambda obs: obs.timestamp
            ),
            addresses=sorted(set(self.addresses) | set(other.addresses)),
            signature=self.signature or other.signature,
            separated=self.separated or other.separated,
            linked_from=sorted(
                set(self.linked_from) | set(other.linked_from) | {self.key, other.key}
            ),
        )


@dataclass(slots=True)
class Criterion:
    """Contribution d'un critère au score final, avec son explication."""

    name: str
    score: float
    weight: float
    detail: str

    @property
    def ratio(self) -> float:
        return self.score / self.weight if self.weight else 0.0


@dataclass(slots=True)
class Verdict:
    """Résultat de l'analyse pour un émetteur."""

    track: DeviceTrack
    score: float
    level: ThreatLevel
    criteria: list[Criterion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.track.label

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "cle": self.track.key,
            "adresses": self.track.addresses,
            "famille": self.track.signature.name if self.track.signature else None,
            "separe_du_proprietaire": self.track.separated,
            "score": round(self.score, 1),
            "niveau": self.level.label,
            "premiere_vue": self.track.first_seen,
            "derniere_vue": self.track.last_seen,
            "observations": len(self.track.observations),
            "criteres": [
                {
                    "nom": crit.name,
                    "score": round(crit.score, 1),
                    "max": crit.weight,
                    "detail": crit.detail,
                }
                for crit in self.criteria
            ],
            "remarques": list(self.notes),
            "recommandations": list(self.recommendations),
        }
