"""Base de signatures BLE des traceurs les plus répandus.

Les identifiants proviennent des attributions publiques du Bluetooth SIG et
de la littérature publique sur les réseaux de localisation collaboratifs.
Chaque signature porte un niveau de confiance : une correspondance
« faible » sert d'indice, jamais de preuve.

La base est volontairement conservatrice. Faire correspondre un identifiant
constructeur entier (par exemple 0x004C pour Apple) signalerait tous les
iPhone du quartier ; on ne retient donc que les sous-types réellement
spécifiques aux traceurs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .models import BleObservation, TrackerSignature, short_uuid

APPLE_COMPANY_ID = 0x004C

# Types de trame Apple transportés dans les données constructeur.
APPLE_TYPE_FIND_MY = 0x12
APPLE_TYPE_PROXIMITY_PAIRING = 0x07
# Longueur du payload Find My : 0x19 = clé publique complète (mode séparé),
# 0x02 = forme courte émise quand le traceur est près de son propriétaire.
APPLE_FIND_MY_SEPARATED_LEN = 0x19
APPLE_FIND_MY_NEARBY_LEN = 0x02

# Trames Eddystone : 0x40 et 0x41 sont utilisées par le réseau
# « Find My Device » de Google, les autres sont des balises classiques.
EDDYSTONE_UUID = "feaa"
FMDN_FRAME_TYPES = (0x40, 0x41)

BATTERY_LABELS = {0: "pleine", 1: "moyenne", 2: "faible", 3: "critique"}


SIGNATURES: tuple[TrackerSignature, ...] = (
    TrackerSignature(
        key="apple_find_my",
        name="Apple Find My / AirTag",
        vendor="Apple",
        mfg_prefixes=((APPLE_COMPANY_ID, bytes([APPLE_TYPE_FIND_MY])),),
        service_uuids=("fd44",),
        rotates_address=True,
        confidence="haute",
        notes=(
            "Trame Find My (type 0x12). Une longueur de 0x19 signifie que "
            "l'objet est séparé de son propriétaire : c'est l'état d'un "
            "traceur laissé sur une cible."
        ),
    ),
    TrackerSignature(
        key="google_fmdn",
        name="Google Find My Device (balise FMDN)",
        vendor="Google",
        service_data_prefixes=tuple(
            (EDDYSTONE_UUID, bytes([frame])) for frame in FMDN_FRAME_TYPES
        ),
        rotates_address=True,
        confidence="haute",
        notes="Trame Eddystone 0xFEAA de type 0x40/0x41, réservée au réseau FMDN.",
    ),
    TrackerSignature(
        key="samsung_smarttag",
        name="Samsung Galaxy SmartTag",
        vendor="Samsung",
        service_uuids=("fd5a", "fd59"),
        rotates_address=True,
        confidence="haute",
        notes="UUID de service enregistrés par Samsung pour SmartThings Find.",
    ),
    TrackerSignature(
        key="tile",
        name="Tile",
        vendor="Tile / Life360",
        service_uuids=("feed", "feec"),
        rotates_address=True,
        confidence="haute",
        notes="0xFEED (appairé) et 0xFEEC (mode appairage) sont attribués à Tile.",
    ),
    TrackerSignature(
        key="chipolo",
        name="Chipolo",
        vendor="Chipolo",
        name_patterns=(r"chipolo",),
        rotates_address=False,
        confidence="moyenne",
        notes=(
            "Détection par nom annoncé. Le Chipolo ONE Spot passe par le "
            "réseau Apple Find My et sort donc sous cette signature-là."
        ),
    ),
    TrackerSignature(
        key="pebblebee",
        name="Pebblebee",
        vendor="Pebblebee",
        name_patterns=(r"pebblebee", r"\bpb[- ]?(found|clip|tag)\b"),
        rotates_address=False,
        confidence="moyenne",
        notes="Détection par nom annoncé.",
    ),
    TrackerSignature(
        key="cellular_gps_tracker",
        name="Traceur GPS cellulaire (interface de configuration)",
        vendor="divers",
        name_patterns=(
            r"^tk\s?\d{3}",
            r"\bgt0?6\b",
            r"coban",
            r"sinotrack",
            r"seeworld",
            r"concox",
            r"topin",
            r"\bgps[-_ ]?track",
            r"\btracker\b",
        ),
        rotates_address=False,
        confidence="faible",
        notes=(
            "Beaucoup de traceurs 2G/4G pour véhicule exposent une interface "
            "BLE de configuration au nom très reconnaissable. Indice utile, "
            "mais un nom se falsifie : à confirmer au balayage RF."
        ),
    ),
    TrackerSignature(
        key="apple_proximity_pairing",
        name="Accessoire Apple non appairé (AirTag neuf ou AirPods)",
        vendor="Apple",
        mfg_prefixes=((APPLE_COMPANY_ID, bytes([APPLE_TYPE_PROXIMITY_PAIRING])),),
        rotates_address=True,
        confidence="faible",
        notes=(
            "Le type 0x07 est partagé par les AirPods et les AirTag non "
            "appairés : à lui seul il ne prouve rien."
        ),
    ),
)


@dataclass(slots=True)
class Identity:
    """Ce qu'une trame nous apprend sur l'émetteur."""

    signature: TrackerSignature | None = None
    separated: bool = False
    battery: str | None = None
    details: str = ""


def iter_apple_tlv(payload: bytes) -> Iterator[tuple[int, bytes]]:
    """Parcourt les blocs (type, valeur) d'un payload constructeur Apple."""
    index = 0
    while index + 1 < len(payload):
        tlv_type = payload[index]
        length = payload[index + 1]
        value = payload[index + 2 : index + 2 + length]
        if len(value) < length:
            return  # trame tronquée : on s'arrête plutôt que d'inventer
        yield tlv_type, value
        index += 2 + length


def decode_apple_find_my(payload: bytes) -> Identity | None:
    """Décode une trame Find My et dit si l'objet est séparé de son maître.

    L'octet de statut encode le type d'appareil et le niveau de batterie sur
    ses deux bits de poids fort (0x10 = AirTag chargé, 0x50 = batterie
    moyenne).
    """
    for tlv_type, value in iter_apple_tlv(payload):
        if tlv_type != APPLE_TYPE_FIND_MY:
            continue
        separated = len(value) >= APPLE_FIND_MY_SEPARATED_LEN
        battery = None
        if value:
            battery = BATTERY_LABELS.get((value[0] >> 6) & 0x03)
        details = (
            "trame Find My complète (objet séparé de son propriétaire)"
            if separated
            else "trame Find My courte (propriétaire à proximité)"
        )
        return Identity(separated=separated, battery=battery, details=details)
    return None


def _matches(signature: TrackerSignature, observation: BleObservation) -> bool:
    for company_id in signature.company_ids:
        if company_id in observation.manufacturer_data:
            return True
    for company_id, prefix in signature.mfg_prefixes:
        payload = observation.manufacturer_data.get(company_id)
        if payload is not None and payload.startswith(prefix):
            return True
    for uuid in signature.service_uuids:
        target = short_uuid(uuid)
        if target in observation.service_uuids or target in observation.service_data:
            return True
    for uuid, prefix in signature.service_data_prefixes:
        payload = observation.service_data.get(short_uuid(uuid))
        if payload is not None and payload.startswith(prefix):
            return True
    if observation.name:
        for pattern in signature.name_patterns:
            if re.search(pattern, observation.name, re.IGNORECASE):
                return True
    return False


def identify(
    observation: BleObservation, catalog: Iterable[TrackerSignature] | None = None
) -> Identity:
    """Identifie la famille de traceur d'une trame BLE.

    Les signatures sont testées dans l'ordre du catalogue, de la plus
    spécifique à la plus générique : la première qui correspond gagne.
    """
    for signature in catalog if catalog is not None else SIGNATURES:
        if not _matches(signature, observation):
            continue
        identity = Identity(signature=signature, details=signature.notes)
        if signature.key == "apple_find_my":
            decoded = decode_apple_find_my(
                observation.manufacturer_data.get(APPLE_COMPANY_ID, b"")
            )
            if decoded is not None:
                identity.separated = decoded.separated
                identity.battery = decoded.battery
                identity.details = decoded.details
        return identity
    return Identity()


def identify_track(
    observations: Iterable[BleObservation],
    catalog: Iterable[TrackerSignature] | None = None,
) -> Identity:
    """Identité consolidée d'un émetteur vu plusieurs fois.

    On garde la signature la plus fiable rencontrée, et l'état « séparé »
    dès qu'une seule trame le signale : un traceur alterne les deux formes
    selon que son propriétaire est ou non à proximité.
    """
    best = Identity()
    for observation in observations:
        current = identify(observation, catalog)
        if current.separated:
            best.separated = True
        if current.signature is None:
            continue
        if best.signature is None or (
            current.signature.confidence_factor > best.signature.confidence_factor
        ):
            best.signature = current.signature
            best.battery = current.battery or best.battery
            best.details = current.details
    return best


def load_custom_signatures(path: str | Path) -> tuple[TrackerSignature, ...]:
    """Charge des signatures supplémentaires depuis un fichier JSON.

    Format attendu : une liste d'objets reprenant les champs de
    `TrackerSignature`, les octets étant écrits en hexadécimal.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    signatures = []
    for entry in raw:
        signatures.append(
            TrackerSignature(
                key=entry["key"],
                name=entry["name"],
                vendor=entry.get("vendor", "inconnu"),
                company_ids=tuple(entry.get("company_ids", ())),
                mfg_prefixes=tuple(
                    (int(cid), bytes.fromhex(prefix))
                    for cid, prefix in entry.get("mfg_prefixes", ())
                ),
                service_uuids=tuple(entry.get("service_uuids", ())),
                service_data_prefixes=tuple(
                    (uuid, bytes.fromhex(prefix))
                    for uuid, prefix in entry.get("service_data_prefixes", ())
                ),
                name_patterns=tuple(entry.get("name_patterns", ())),
                rotates_address=bool(entry.get("rotates_address", True)),
                confidence=entry.get("confidence", "moyenne"),
                notes=entry.get("notes", ""),
            )
        )
    return tuple(signatures)
