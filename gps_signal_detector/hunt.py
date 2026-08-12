"""Mode traque : écoute permanente et affichage temps réel dans le terminal.

Un seul processus, qui ne s'arrête pas : le scan BLE tourne en continu, et
l'affichage se rafraîchit plusieurs fois par seconde. On ne relance rien —
on bouge l'ordinateur et on regarde la jauge.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from dataclasses import dataclass, field

from .hunt_view import render_hunt, render_picker
from .live_state import LiveDevice, LiveState
from .models import BleObservation

REFRESH_S = 0.15

# Trame Find My complète (objet séparé de son propriétaire).
_DEMO_FIND_MY = b"\x12\x19" + bytes([0x10]) + bytes(24)


@dataclass
class HuntSession:
    """État de l'interface : ce qu'on regarde et ce qu'on a sélectionné."""

    state: LiveState
    mode: str = "picker"  # "picker" ou "hunt"
    selected: int = 0
    target: str | None = None
    trackers_only: bool = False
    status: str = ""

    def devices(self, now: float) -> list[LiveDevice]:
        return self.state.ranked(now, self.trackers_only)

    def current_device(self) -> LiveDevice | None:
        if self.target is None:
            return None
        return self.state.devices.get(self.target)

    def cycle_target(self, now: float, step: int = 1) -> None:
        devices = self.devices(now)
        if not devices:
            return
        addresses = [device.address for device in devices]
        try:
            index = addresses.index(self.target) if self.target else -1
        except ValueError:
            index = -1
        self.target = addresses[(index + step) % len(addresses)]


# --- Sources de mesures ----------------------------------------------


async def ble_feed(
    state: LiveState,
    stop: asyncio.Event,
    adapter: str | None = None,
    restart_scan_s: float = 0.0,
) -> None:  # pragma: no cover - demande un adaptateur Bluetooth
    """Scan BLE permanent, alimentant l'état au fil des trames.

    `restart_scan_s` relance périodiquement le scan. C'est la parade quand
    la plateforme déduplique les annonces et ne signale un appareil qu'une
    seule fois : sans nouvelle découverte, le RSSI resterait figé.
    """
    from .ble_scanner import _require_bleak

    scanner_class = _require_bleak()

    def on_detection(device, advertisement) -> None:
        address = (device.address or "").upper()
        if not address:
            return
        rssi = getattr(advertisement, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", -100)
        state.record(
            BleObservation(
                timestamp=time.time(),
                address=address,
                rssi=int(rssi),
                name=advertisement.local_name or getattr(device, "name", None),
                manufacturer_data=dict(advertisement.manufacturer_data or {}),
                service_uuids=tuple(advertisement.service_uuids or ()),
                service_data=dict(advertisement.service_data or {}),
            )
        )

    kwargs = {"detection_callback": on_detection}
    if adapter:
        kwargs["adapter"] = adapter

    while not stop.is_set():
        scanner = scanner_class(**kwargs)
        await scanner.start()
        try:
            if restart_scan_s > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=restart_scan_s)
            else:
                await stop.wait()
        finally:
            with contextlib.suppress(Exception):
                await scanner.stop()
        if restart_scan_s <= 0:
            break


@dataclass(frozen=True)
class _DemoDevice:
    """Appareil simulé, avec sa propre cadence d'émission."""

    address: str
    period_s: float
    name: str | None = None
    manufacturer_data: dict = field(default_factory=dict)
    service_uuids: tuple = ()
    moving: bool = False
    base_rssi: float = -75.0


DEMO_DEVICES: tuple[_DemoDevice, ...] = (
    # Cadence de 2 s : c'est celle d'un vrai AirTag, et elle borne la
    # réactivité de la jauge. Autant que la démonstration le montre.
    _DemoDevice(
        "7A:2B:3C:4D:5E:6F",
        period_s=2.0,
        manufacturer_data={0x004C: _DEMO_FIND_MY},
        moving=True,
    ),
    _DemoDevice("C4:19:8A:02:77:31", period_s=1.0, name="Tile", service_uuids=("feed",),
                base_rssi=-78.0),
    _DemoDevice("D2:55:1B:9C:44:0E", period_s=5.0, name="Enceinte du salon",
                base_rssi=-58.0),
    _DemoDevice("6B:71:E0:12:9F:AA", period_s=3.0, name="Téléphone",
                manufacturer_data={0x004C: b"\x10\x05\x01\x02\x03\x04\x05"},
                base_rssi=-88.0),
)


async def demo_feed(state: LiveState, stop: asyncio.Event, seed: int = 42) -> None:
    """Alimente l'état avec un scénario simulé : aucun matériel requis.

    Le traceur simulé se rapproche puis s'éloigne selon un cycle d'environ
    trente secondes, comme si l'on tournait autour de sa cachette.
    """
    rng = random.Random(seed)
    started = time.time()
    schedule = {device.address: started for device in DEMO_DEVICES}

    while not stop.is_set():
        now = time.time()
        for spec in DEMO_DEVICES:
            if now < schedule[spec.address]:
                continue
            schedule[spec.address] = now + spec.period_s * rng.uniform(0.85, 1.15)

            if spec.moving:
                phase = (now - started) / 30.0
                closeness = (math.sin(phase * 2 * math.pi) + 1) / 2
                rssi = -95 + closeness * 52 + rng.gauss(0, 2.5)
            else:
                rssi = spec.base_rssi + rng.gauss(0, 3.0)

            state.record(
                BleObservation(
                    timestamp=now,
                    address=spec.address,
                    rssi=int(round(rssi)),
                    name=spec.name,
                    manufacturer_data=dict(spec.manufacturer_data),
                    service_uuids=spec.service_uuids,
                )
            )
        await asyncio.sleep(0.1)


# --- Affichage curses -------------------------------------------------


def _init_colors(curses) -> dict[str, int]:
    """Associe chaque style à un attribut du terminal."""
    styles = {
        "": curses.A_NORMAL,
        "flat": curses.A_NORMAL,
        "value": curses.A_BOLD,
        "title": curses.A_BOLD,
        "dim": curses.A_DIM,
        "selected": curses.A_REVERSE,
        "hot": curses.A_BOLD,
        "warm": curses.A_NORMAL,
        "cool": curses.A_NORMAL,
        "cold": curses.A_BOLD,
        "alert": curses.A_BOLD,
        "curve": curses.A_NORMAL,
    }
    if not curses.has_colors():
        return styles

    curses.start_color()
    with contextlib.suppress(Exception):
        curses.use_default_colors()

    palette = [
        ("hot", curses.COLOR_GREEN),
        ("warm", curses.COLOR_GREEN),
        ("cool", curses.COLOR_YELLOW),
        ("cold", curses.COLOR_RED),
        ("alert", curses.COLOR_RED),
        ("curve", curses.COLOR_CYAN),
        ("title", curses.COLOR_CYAN),
    ]
    for index, (style, colour) in enumerate(palette, start=1):
        with contextlib.suppress(Exception):
            curses.init_pair(index, colour, -1)
            styles[style] |= curses.color_pair(index)
    return styles


def _handle_keys(curses, stdscr, session: HuntSession, now: float, stop: asyncio.Event) -> None:
    """Traite toutes les touches en attente sans jamais bloquer."""
    while True:
        key = stdscr.getch()
        if key == -1:
            return

        if key in (ord("q"), 27):  # 27 = Échap
            if session.mode == "hunt":
                session.mode = "picker"
                session.status = ""
            else:
                stop.set()
            continue

        if session.mode == "picker":
            devices = session.devices(now)
            if key in (curses.KEY_DOWN, ord("j")):
                session.selected = min(session.selected + 1, max(0, len(devices) - 1))
            elif key in (curses.KEY_UP, ord("k")):
                session.selected = max(0, session.selected - 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if devices:
                    session.target = devices[min(session.selected, len(devices) - 1)].address
                    session.mode = "hunt"
            elif key == ord("f"):
                session.trackers_only = not session.trackers_only
                session.selected = 0
            continue

        device = session.current_device()
        if device is None:
            continue
        if key == ord("m"):
            mark = device.mark(now)
            session.status = (
                f" Point {mark.index} relevé à {mark.rssi:.0f} dBm"
                if mark is not None
                else " Pas de mesure récente : impossible de relever un point."
            )
        elif key == ord("r"):
            device.reset_calibration()
            session.status = " Calibration remise à zéro."
        elif key == 9:  # Tabulation
            session.cycle_target(now)
            session.status = ""


async def _ui_loop(curses, stdscr, session: HuntSession, stop: asyncio.Event) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    styles = _init_colors(curses)

    while not stop.is_set():
        now = time.time()
        session.state.prune(now)
        height, width = stdscr.getmaxyx()

        if session.mode == "hunt":
            device = session.current_device()
            if device is None:
                session.mode = "picker"
                continue
            device.update_best(now)
            lines = render_hunt(device, now, width, session.state.samples_per_device(now))
        else:
            lines = render_picker(
                session.state, now, session.selected, width, session.trackers_only
            )

        stdscr.erase()
        for row, line in enumerate(lines[: max(0, height - 1)]):
            with contextlib.suppress(Exception):
                stdscr.addnstr(
                    row, 0, line.text, max(0, width - 1), styles.get(line.style, 0)
                )
        if session.status:
            with contextlib.suppress(Exception):
                stdscr.addnstr(
                    height - 1, 0, session.status, max(0, width - 1), styles.get("alert", 0)
                )
        stdscr.refresh()

        _handle_keys(curses, stdscr, session, now, stop)
        await asyncio.sleep(REFRESH_S)


async def _guarded(factory, session: HuntSession) -> None:
    """Isole la source de mesures : une panne d'acquisition ne doit pas
    faire tomber l'affichage, seulement s'y afficher."""
    try:
        await factory()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        session.status = f" Acquisition interrompue : {exc}"


async def _main(
    curses,
    stdscr,
    demo: bool,
    address: str | None,
    adapter: str | None,
    restart_scan_s: float,
    seed: int,
) -> int:
    state = LiveState()
    session = HuntSession(state=state)
    if address:
        session.target = address.upper()
        session.mode = "hunt"

    stop = asyncio.Event()
    if demo:
        factory = lambda: demo_feed(state, stop, seed)  # noqa: E731
    else:
        factory = lambda: ble_feed(state, stop, adapter, restart_scan_s)  # noqa: E731

    feed = asyncio.create_task(_guarded(factory, session))
    try:
        await _ui_loop(curses, stdscr, session, stop)
    finally:
        stop.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feed
    return 0


def run_hunt(
    demo: bool = False,
    address: str | None = None,
    adapter: str | None = None,
    restart_scan_s: float = 0.0,
    seed: int = 42,
) -> int:
    """Lance le mode traque. Rend la main quand l'utilisateur quitte."""
    try:
        import curses
    except ImportError as exc:  # pragma: no cover - Windows sans windows-curses
        raise RuntimeError(
            "Le mode traque a besoin du module curses. Sous Windows : "
            "pip install windows-curses"
        ) from exc

    # On vérifie la disponibilité du Bluetooth *avant* de basculer en plein
    # écran : sinon le message d'erreur serait effacé par curses.
    if not demo:
        from .ble_scanner import _require_bleak

        _require_bleak()

    return curses.wrapper(
        lambda stdscr: asyncio.run(
            _main(curses, stdscr, demo, address, adapter, restart_scan_s, seed)
        )
    )
