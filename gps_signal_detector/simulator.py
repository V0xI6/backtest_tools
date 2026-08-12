"""Générateur de scénarios : permet d'éprouver le détecteur sans matériel.

Le scénario par défaut reproduit une situation réaliste : une immobilisation
au domicile, puis un trajet routier, avec un vrai traceur embarqué au milieu
d'appareils parfaitement innocents. C'est ce jeu de données que la
commande `demo` analyse.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .geo import Position, offset
from .models import BleObservation, SafeZone

HOME = (48.8566, 2.3522)  # Paris, place de l'Hôtel-de-Ville


@dataclass
class Scenario:
    """Jeu d'observations synthétiques et sa vérité terrain."""

    observations: list[BleObservation] = field(default_factory=list)
    scan_times: list[float] = field(default_factory=list)
    safe_zones: list[SafeZone] = field(default_factory=list)
    truth: dict[str, str] = field(default_factory=dict)  # étiquette -> rôle


def _apple_find_my_payload(rng: random.Random, separated: bool) -> bytes:
    """Trame Find My : longue (objet séparé) ou courte (maître à proximité)."""
    if separated:
        status = 0x10  # AirTag, batterie pleine
        body = bytes([status]) + bytes(rng.getrandbits(8) for _ in range(24))
        return b"\x12\x19" + body
    return b"\x12\x02" + bytes([0x00, rng.getrandbits(8)])


def _random_address(rng: random.Random, private: bool = True) -> str:
    first = rng.getrandbits(6) | (0b01 << 6 if private else 0b10 << 6)
    octets = [first] + [rng.getrandbits(8) for _ in range(5)]
    return ":".join(f"{octet:02X}" for octet in octets)


def build_scenario(
    seed: int = 42,
    home_cycles: int = 20,
    drive_cycles: int = 70,
    cycle_s: float = 30.0,
    speed_kmh: float = 50.0,
    start_time: float = 1_700_000_000.0,
    home: Position = HOME,
) -> Scenario:
    """Construit un trajet complet avec traceurs et appareils de décor.

    Rôles présents : un AirTag en mode séparé qui fait tourner son adresse
    MAC, un traceur cellulaire au nom bavard, une enceinte du domicile, une
    balise de magasin croisée en route et un téléphone de passage.
    """
    rng = random.Random(seed)
    scenario = Scenario()
    step_m = speed_kmh * 1000 / 3600 * cycle_s

    # Trajectoire de l'observateur : immobile au domicile, puis en route.
    route: list[Position] = [home] * home_cycles
    position = home
    for index in range(drive_cycles):
        # Virage à mi-parcours pour créer des zones vraiment distinctes.
        north, east = (step_m, 0.0) if index < drive_cycles // 2 else (0.0, step_m)
        position = offset(position, north, east)
        route.append(position)

    scenario.safe_zones = [SafeZone("Domicile", home[0], home[1], radius_m=200.0)]
    scenario.truth = {
        # L'AirTag n'annonce aucun nom : il ressort sous le nom de sa famille.
        "Apple Find My / AirTag": "traceur",
        "TK905 GPS Tracker": "traceur",
        "Enceinte du salon": "décor",
        "Tile de la boutique": "décor",
        "Téléphone croisé": "décor",
    }

    # Le traceur Find My change d'adresse toutes les quinze minutes.
    rotation_cycles = max(1, int(900 / cycle_s))
    airtag_address = _random_address(rng)
    speaker_address = _random_address(rng, private=False)
    tile_address = _random_address(rng, private=False)
    tracker_address = _random_address(rng, private=False)
    phone_address = _random_address(rng)

    for cycle, observer in enumerate(route):
        timestamp = start_time + cycle * cycle_s
        scenario.scan_times.append(timestamp)

        if cycle and cycle % rotation_cycles == 0:
            airtag_address = _random_address(rng)

        # 1. Le traceur embarqué : présent à chaque scan, signal fort.
        scenario.observations.append(
            BleObservation(
                timestamp=timestamp,
                address=airtag_address,
                rssi=-62 + rng.randint(-6, 6),
                manufacturer_data={0x004C: _apple_find_my_payload(rng, separated=True)},
                latitude=observer[0],
                longitude=observer[1],
                address_type="random",
            )
        )

        # 2. Traceur cellulaire : même comportement, identifié par son nom.
        if rng.random() < 0.9:  # quelques trames perdues, comme en vrai
            scenario.observations.append(
                BleObservation(
                    timestamp=timestamp,
                    address=tracker_address,
                    rssi=-70 + rng.randint(-8, 8),
                    name="TK905 GPS Tracker",
                    latitude=observer[0],
                    longitude=observer[1],
                )
            )

        # 3. Enceinte du domicile : vue longtemps, mais jamais ailleurs.
        if cycle < home_cycles:
            scenario.observations.append(
                BleObservation(
                    timestamp=timestamp,
                    address=speaker_address,
                    rssi=-55 + rng.randint(-5, 5),
                    name="Enceinte du salon",
                    latitude=observer[0],
                    longitude=observer[1],
                )
            )

        # 4. Tile d'une boutique traversée : signature connue, un seul lieu.
        if home_cycles + 20 <= cycle < home_cycles + 24:
            scenario.observations.append(
                BleObservation(
                    timestamp=timestamp,
                    address=tile_address,
                    rssi=-80 + rng.randint(-8, 8),
                    name="Tile de la boutique",
                    service_uuids=("feed",),
                    latitude=observer[0],
                    longitude=observer[1],
                )
            )

        # 5. Téléphone croisé à un feu rouge : deux trames, puis plus rien.
        if home_cycles + 40 <= cycle < home_cycles + 42:
            scenario.observations.append(
                BleObservation(
                    timestamp=timestamp,
                    address=phone_address,
                    rssi=-88 + rng.randint(-6, 6),
                    name="Téléphone croisé",
                    # Type Apple 0x10 (« nearby info ») : un iPhone banal,
                    # qui ne doit surtout pas être pris pour un traceur.
                    manufacturer_data={0x004C: b"\x10\x05\x01\x02\x03\x04\x05"},
                    latitude=observer[0],
                    longitude=observer[1],
                )
            )

    return scenario
