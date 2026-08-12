"""Acquisition des trames BLE via bleak.

Une seule observation est conservée par appareil et par cycle de scan : le
taux de présence calculé ensuite compare des cycles, pas des trames. Un
AirTag émet toutes les deux secondes, il saturerait sinon les statistiques
face à un appareil plus bavard.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Iterable, Sequence

from .models import BleObservation
from .position import PositionProvider


class ScannerUnavailable(RuntimeError):
    """bleak absent, ou aucun adaptateur Bluetooth exploitable."""


def _require_bleak():
    try:
        from bleak import BleakScanner  # type: ignore
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise ScannerUnavailable(
            "Le scan BLE demande bleak : pip install bleak\n"
            "Sans matériel, utilisez la commande « demo » pour éprouver "
            "le détecteur sur un scénario simulé."
        ) from exc
    return BleakScanner


async def scan_cycle(
    duration_s: float = 8.0,
    position: tuple[float, float] | None = None,
    adapter: str | None = None,
) -> list[BleObservation]:
    """Écoute les annonces BLE pendant `duration_s` et les agrège.

    En cas de trames multiples pour un même appareil, on garde la plus
    forte : c'est celle qui reflète le mieux la distance réelle.
    """
    scanner_class = _require_bleak()
    strongest: dict[str, BleObservation] = {}
    timestamp = time.time()

    def on_detection(device, advertisement) -> None:
        address = (device.address or "").upper()
        if not address:
            return
        rssi = getattr(advertisement, "rssi", None)
        if rssi is None:  # pragma: no cover - anciennes versions de bleak
            rssi = getattr(device, "rssi", -100)
        previous = strongest.get(address)
        if previous is not None and previous.rssi >= rssi:
            return
        strongest[address] = BleObservation(
            timestamp=timestamp,
            address=address,
            rssi=int(rssi),
            name=advertisement.local_name or getattr(device, "name", None),
            manufacturer_data=dict(advertisement.manufacturer_data or {}),
            service_uuids=tuple(advertisement.service_uuids or ()),
            service_data=dict(advertisement.service_data or {}),
            latitude=position[0] if position else None,
            longitude=position[1] if position else None,
        )

    kwargs = {"detection_callback": on_detection}
    if adapter:
        kwargs["adapter"] = adapter
    scanner = scanner_class(**kwargs)

    await scanner.start()
    try:
        await asyncio.sleep(duration_s)
    finally:
        await scanner.stop()
    return list(strongest.values())


async def monitor(
    cycles: int = 20,
    interval_s: float = 30.0,
    duration_s: float = 8.0,
    position_provider: PositionProvider | None = None,
    adapter: str | None = None,
    on_cycle: Callable[[int, Sequence[BleObservation]], None] | None = None,
) -> tuple[list[BleObservation], list[float]]:
    """Enchaîne `cycles` scans et renvoie (observations, instants de scan).

    `cycles <= 0` fait tourner la surveillance jusqu'à interruption clavier,
    ce qui correspond à l'usage réel : on lance l'outil et on roule.
    """
    observations: list[BleObservation] = []
    scan_times: list[float] = []
    index = 0

    try:
        while cycles <= 0 or index < cycles:
            position = position_provider() if position_provider is not None else None
            started = time.time()
            batch = await scan_cycle(duration_s, position, adapter)
            scan_times.append(started)
            observations.extend(batch)
            index += 1
            if on_cycle is not None:
                on_cycle(index, batch)
            if cycles <= 0 or index < cycles:
                await asyncio.sleep(max(0.0, interval_s - (time.time() - started)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return observations, scan_times


def run_monitor(
    cycles: int = 20,
    interval_s: float = 30.0,
    duration_s: float = 8.0,
    position_provider: PositionProvider | None = None,
    adapter: str | None = None,
    on_cycle: Callable[[int, Sequence[BleObservation]], None] | None = None,
) -> tuple[list[BleObservation], list[float]]:
    """Version synchrone de `monitor`, pour la ligne de commande."""
    try:
        return asyncio.run(
            monitor(cycles, interval_s, duration_s, position_provider, adapter, on_cycle)
        )
    except KeyboardInterrupt:  # pragma: no cover - interaction clavier
        return [], []


def merge_cycles(batches: Iterable[Sequence[BleObservation]]) -> list[BleObservation]:
    """Aplatit des lots de scans en une seule liste triée par date."""
    merged = [observation for batch in batches for observation in batch]
    return sorted(merged, key=lambda observation: observation.timestamp)
