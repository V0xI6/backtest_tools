"""Sources de position pour horodater géographiquement les scans BLE.

Sans position, le détecteur perd ses deux critères les plus décisifs. Trois
sources sont proposées, de la plus simple à la plus courante sur le
terrain : position fixe, flux NMEA (récepteur USB, fichier rejoué) et
gpsd.

Le flux NMEA alimente au passage un `GnssMonitor`, ce qui permet de
surveiller le brouillage avec le même récepteur, sans matériel en plus.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Iterator, Protocol

from .gnss_monitor import GnssConfig, GnssMonitor, GnssVerdict

Position = tuple[float, float]


class PositionProvider(Protocol):
    """Tout appelable renvoyant notre position courante, ou None."""

    def __call__(self) -> Position | None: ...


class StaticPosition:
    """Position figée : utile en intérieur ou pour rejouer un jeu de test."""

    def __init__(self, latitude: float, longitude: float) -> None:
        self.position = (latitude, longitude)

    def __call__(self) -> Position | None:
        return self.position


class _ThreadedProvider:
    """Base commune : un fil lit le flux en continu, l'appelant lit la dernière position."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._position: Position | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def _run(self) -> None:  # pragma: no cover - dépend du matériel
        raise NotImplementedError

    def start(self) -> "_ThreadedProvider":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._guarded_run, daemon=True)
            self._thread.start()
        return self

    def _guarded_run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # le scan BLE doit survivre à une panne GPS
            self.error = exc

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _publish(self, position: Position | None) -> None:
        if position is not None:
            with self._lock:
                self._position = position

    def __call__(self) -> Position | None:
        with self._lock:
            return self._position

    def __enter__(self) -> "_ThreadedProvider":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


class NmeaPositionProvider(_ThreadedProvider):
    """Lit un flux NMEA depuis un port série ou un fichier.

    `source` accepte un chemin de périphérique (`/dev/ttyACM0`, `COM3`,
    auquel cas pyserial est requis) ou un fichier de journal à rejouer.
    """

    def __init__(
        self,
        source: str,
        baudrate: int = 9600,
        config: GnssConfig | None = None,
        loop_file: bool = False,
    ) -> None:
        super().__init__()
        self.source = source
        self.baudrate = baudrate
        self.loop_file = loop_file
        self.monitor = GnssMonitor(config)

    @property
    def is_serial(self) -> bool:
        return self.source.startswith("/dev/") or self.source.upper().startswith("COM")

    def _lines(self) -> Iterator[str]:  # pragma: no cover - dépend du matériel
        if self.is_serial:
            try:
                import serial  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "La lecture d'un port série demande pyserial : "
                    "pip install pyserial"
                ) from exc
            with serial.Serial(self.source, self.baudrate, timeout=1) as port:
                while not self._stop.is_set():
                    raw = port.readline()
                    if raw:
                        yield raw.decode("ascii", errors="ignore")
        else:
            while not self._stop.is_set():
                with open(self.source, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        if self._stop.is_set():
                            return
                        yield line
                if not self.loop_file:
                    return

    def _run(self) -> None:  # pragma: no cover - dépend du matériel
        for line in self._lines():
            if self.monitor.feed(line):
                self._publish(self.monitor.snapshot.position)

    def assess(self) -> GnssVerdict:
        """Verdict courant sur la santé du signal GNSS."""
        return self.monitor.assess()


class GpsdPositionProvider(_ThreadedProvider):
    """Client gpsd minimal (protocole JSON), sans dépendance externe."""

    def __init__(self, host: str = "127.0.0.1", port: int = 2947) -> None:
        super().__init__()
        self.host = host
        self.port = port

    def _run(self) -> None:  # pragma: no cover - dépend d'un démon externe
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buffer = b""
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    self._handle(line)

    def _handle(self, line: bytes) -> None:  # pragma: no cover - idem
        try:
            report = json.loads(line.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return
        if report.get("class") == "TPV":
            latitude, longitude = report.get("lat"), report.get("lon")
            if latitude is not None and longitude is not None:
                self._publish((float(latitude), float(longitude)))


def build_provider(spec: str | None) -> PositionProvider | None:
    """Construit une source de position à partir d'une chaîne de commande.

    Formats acceptés : "gpsd", "gpsd:hôte:port", "48.85,2.35" (position
    fixe) ou un chemin vers un port série / un fichier NMEA.
    """
    if not spec:
        return None
    if spec == "gpsd":
        return GpsdPositionProvider().start()
    if spec.startswith("gpsd:"):
        _, _, remainder = spec.partition(":")
        host, _, port = remainder.partition(":")
        return GpsdPositionProvider(host or "127.0.0.1", int(port or 2947)).start()
    if "," in spec and not spec.startswith("/"):
        latitude, _, longitude = spec.partition(",")
        return StaticPosition(float(latitude), float(longitude))
    return NmeaPositionProvider(spec).start()
