"""Interface graphique locale, servie dans le navigateur.

Deux responsabilités, séparées à dessein :

1. **Traduire.** L'état interne parle en dBm, en RSSI et en identifiants
   constructeur. Personne ne cherche un mouchard sous son pare-chocs en
   pensant en décibels-milliwatts. Tout est donc converti en langage
   courant — un signal en pourcentage, « Tout près », « Vous vous
   rapprochez » — et les valeurs brutes sont reléguées dans un repli
   « détails techniques » pour qui les veut.
2. **Servir.** Un serveur HTTP de la bibliothèque standard, lié à
   127.0.0.1, qui expose la page et un point d'accès JSON interrogé en
   continu. Rien ne sort de la machine.

La traduction est une fonction pure : elle se teste sans navigateur.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .live_state import (
    HISTORY_WINDOW_S,
    LiveDevice,
    LiveState,
    bucketed_series,
    proximity_band,
    signal_ratio,
)

WEB_ROOT = Path(__file__).parent / "web"
HISTORY_POINTS = 72

# Trois niveaux de gravité seulement. Les cinq bandes de proximité restent
# distinguées par leur texte : la couleur ne porte jamais seule le sens, et
# une palette à trois pas se discrimine sans ambiguïté, y compris pour un
# œil daltonien.
SEVERITY_BY_BAND = {
    "À PORTÉE DE MAIN": "critique",
    "TRÈS PROCHE": "attention",
    "PROCHE": "attention",
    "À DISTANCE": "calme",
    "LIMITE DE PORTÉE": "calme",
}

PLAIN_BAND_NAMES = {
    "À PORTÉE DE MAIN": "Juste à côté de vous",
    "TRÈS PROCHE": "Tout près",
    "PROCHE": "Dans les environs",
    "À DISTANCE": "Assez loin",
    "LIMITE DE PORTÉE": "Tout au bord de la portée",
}

# Nom affiché quand l'appareil n'annonce pas le sien. Les noms internes
# (« Apple Find My ») sont de l'anglais technique : ils n'ont rien à faire
# sous les yeux de quelqu'un qui fouille son coffre.
PLAIN_SHORT_NAMES = {
    "apple_find_my": "Traceur Apple AirTag",
    "google_fmdn": "Balise Google",
    "samsung_smarttag": "Traceur Samsung SmartTag",
    "tile": "Traceur Tile",
    "chipolo": "Traceur Chipolo",
    "pebblebee": "Traceur Pebblebee",
    "cellular_gps_tracker": "Traceur GPS avec carte SIM",
    "apple_proximity_pairing": "Accessoire Apple",
}

# Ligne d'explication sous le nom : à quel réseau l'objet appartient, ou
# comment il a été reconnu. Complète le nom au lieu de le répéter.
PLAIN_TYPE_NAMES = {
    "apple_find_my": "Réseau « Localiser » d'Apple",
    "google_fmdn": "Réseau « Localiser mon appareil » de Google",
    "samsung_smarttag": "Réseau « SmartThings Find » de Samsung",
    "tile": "Réseau Tile",
    "chipolo": "Traceur Chipolo, reconnu à son nom",
    "pebblebee": "Traceur Pebblebee, reconnu à son nom",
    "cellular_gps_tracker": "Boîtier GPS à carte SIM, reconnu à son nom",
    "apple_proximity_pairing": "Accessoire Apple non appairé — AirPods ou AirTag neuf",
}

UNKNOWN_TYPE = "Appareil Bluetooth non identifié"


def plain_trend(delta: float | None) -> dict:
    """Traduit une variation en dB en une phrase compréhensible."""
    if delta is None:
        return {"texte": "Mesure en cours…", "sens": "stable", "niveau": 0}
    if delta >= 3:
        return {"texte": "Vous vous rapprochez", "sens": "chaud", "niveau": 2}
    if delta >= 1:
        return {"texte": "Ça se rapproche un peu", "sens": "chaud", "niveau": 1}
    if delta > -1:
        return {"texte": "Stable — ne bougez plus", "sens": "stable", "niveau": 0}
    if delta > -3:
        return {"texte": "Ça s'éloigne un peu", "sens": "froid", "niveau": -1}
    return {"texte": "Vous vous éloignez", "sens": "froid", "niveau": -2}


def signal_percent(rssi: float | None) -> int | None:
    """Puissance ramenée à un pourcentage lisible par tout le monde."""
    if rssi is None:
        return None
    return int(round(signal_ratio(rssi) * 100))


def describe_device(device: LiveDevice, now: float) -> dict:
    """Décrit un appareil en langage courant, sans jargon."""
    smoothed = device.smoothed(now)
    percent = signal_percent(smoothed)

    if smoothed is None:
        band, hint, severity, plain = None, None, "calme", "Silencieux"
    else:
        band, hint = proximity_band(smoothed)
        severity = SEVERITY_BY_BAND.get(band, "calme")
        plain = PLAIN_BAND_NAMES.get(band, band)

    signature = device.signature
    if device.name:
        name = device.name
    elif signature is not None:
        name = PLAIN_SHORT_NAMES.get(signature.key, signature.name)
    else:
        name = device.address
    kind = PLAIN_TYPE_NAMES.get(signature.key, signature.name) if signature else UNKNOWN_TYPE

    alerte = None
    if device.separated:
        alerte = "Cet objet signale sa position à son propriétaire, à distance."
    elif signature is not None and signature.confidence == "faible":
        alerte = "Reconnu d'après son nom seulement : à confirmer."

    history = bucketed_series(device.samples, now, HISTORY_WINDOW_S, HISTORY_POINTS)

    return {
        "id": device.address,
        "nom": name,
        "type": kind,
        "traceur": signature is not None,
        "alerte": alerte,
        "signal": percent,
        "proximite": plain,
        "indice": hint,
        "severite": severity,
        "muet": device.is_stale(now),
        "vu_il_y_a": round(device.age(now), 1),
        "tendance": plain_trend(device.trend(now)),
        "historique": [signal_percent(value) for value in history],
        "meilleur": signal_percent(device.best_rssi),
        "points": [
            {"n": mark.index, "signal": signal_percent(mark.rssi)} for mark in device.marks
        ],
        "technique": {
            "identifiant": device.address,
            "puissance_dbm": round(smoothed, 1) if smoothed is not None else None,
            "mesures": device.count,
            "cadence": round(device.rate(now), 1),
            "batterie": device.battery,
            "famille": signature.name if signature else None,
        },
    }


def snapshot(state: LiveState, now: float, scanning: bool = True) -> dict:
    """État complet destiné à la page."""
    devices = state.ranked(now)
    described = [describe_device(device, now) for device in devices]

    avertissement = None
    density = state.samples_per_device(now)
    if devices and 0 < density <= 1.2:
        avertissement = {
            "titre": "Le suivi en direct ne fonctionne pas sur cette machine",
            "texte": (
                "Votre système ne signale chaque appareil qu'une seule fois, "
                "donc le signal ne bougera pas quand vous vous déplacerez. "
                "Relancez la commande en ajoutant : --restart-scan 2"
            ),
        }

    return {
        "instant": now,
        "ecoute": scanning,
        "total": len(described),
        "traceurs": sum(1 for entry in described if entry["traceur"]),
        "avertissement": avertissement,
        "appareils": described,
    }


class _Handler(BaseHTTPRequestHandler):
    """Sert la page et le point d'accès JSON."""

    server_version = "DetecteurTraceurs/1.2"

    def __init__(self, state: LiveState, scanning_flag, *args, **kwargs) -> None:
        self.state = state
        self.scanning_flag = scanning_flag
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:  # le terminal reste lisible
        pass

    def _send(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: dict, status: int = 200) -> None:
        self._send(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:
        import time

        if self.path in ("/", "/index.html"):
            page = (WEB_ROOT / "index.html").read_bytes()
            self._send(page, "text/html; charset=utf-8")
        elif self.path.startswith("/api/etat"):
            self._send_json(snapshot(self.state, time.time(), self.scanning_flag()))
        else:
            self._send_json({"erreur": "introuvable"}, 404)

    def do_POST(self) -> None:
        import time

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"erreur": "corps illisible"}, 400)
            return

        device = self.state.devices.get(str(body.get("id", "")).upper())
        if device is None:
            self._send_json({"erreur": "appareil inconnu"}, 404)
            return

        now = time.time()
        if self.path == "/api/marquer":
            mark = device.mark(now)
            self._send_json(
                {"ok": mark is not None, "signal": signal_percent(mark.rssi) if mark else None}
            )
        elif self.path == "/api/reinitialiser":
            device.reset_calibration()
            self._send_json({"ok": True})
        else:
            self._send_json({"erreur": "introuvable"}, 404)


def serve(
    state: LiveState,
    host: str = "127.0.0.1",
    port: int = 8765,
    scanning_flag=lambda: True,
) -> ThreadingHTTPServer:
    """Démarre le serveur dans un fil et rend la main immédiatement."""
    handler = partial(_Handler, state, scanning_flag)
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def run_web(
    demo: bool = False,
    adapter: str | None = None,
    restart_scan_s: float = 0.0,
    host: str = "127.0.0.1",
    port: int = 8765,
    seed: int = 42,
    open_browser: bool = True,
) -> int:
    """Lance l'acquisition et l'interface, jusqu'à interruption clavier."""
    import asyncio
    import sys
    import webbrowser

    from .hunt import ble_feed, demo_feed

    # Vérifié avant de démarrer le serveur : mieux vaut un message clair
    # qu'une page vide qui n'expliquerait rien.
    if not demo:
        from .ble_scanner import _require_bleak

        _require_bleak()

    state = LiveState()
    httpd = serve(state, host, port)
    url = f"http://{host}:{port}/"

    print(f"Interface disponible sur {url}", file=sys.stderr)
    print("Laissez tourner et déplacez-vous. Ctrl-C pour arrêter.", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)

    async def acquisition() -> None:
        stop = asyncio.Event()
        if demo:
            await demo_feed(state, stop, seed)
        else:
            await ble_feed(state, stop, adapter, restart_scan_s)

    try:
        asyncio.run(acquisition())
    except KeyboardInterrupt:
        print("\nArrêt.", file=sys.stderr)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
