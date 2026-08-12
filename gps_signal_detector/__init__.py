"""Détecteur de traceurs GPS.

Repère les balises Bluetooth qui vous suivent (AirTag, Tile, SmartTag,
balises Google FMDN, traceurs cellulaires), surveille le brouillage et le
leurrage du signal GNSS, et balaie les bandes montantes cellulaires à la
recherche d'un émetteur périodique.

Prise en main sans matériel :

    python -m gps_signal_detector demo

Utilisation en bibliothèque :

    from gps_signal_detector import TrackingAnalyzer, render_text

    verdicts = TrackingAnalyzer().analyze(observations, scan_times)
    print(render_text(verdicts, observations, scan_times))
"""

from .geo import cluster_positions, haversine, path_length
from .gnss_monitor import GnssConfig, GnssMonitor, GnssStatus, GnssVerdict
from .hunt_view import render_hunt, render_picker
from .live_state import LiveDevice, LiveState, proximity_band, trend_label
from .models import (
    BleObservation,
    Criterion,
    DeviceTrack,
    SafeZone,
    ThreatLevel,
    TrackerSignature,
    Verdict,
)
from .report import render_html, render_json, render_text, summarise
from .signatures import SIGNATURES, identify, identify_track
from .simulator import build_scenario
from .storage import Database
from .tracking_analyzer import (
    DetectionConfig,
    TrackingAnalyzer,
    Weights,
    build_tracks,
    link_identities,
)

__version__ = "1.1.0"

__all__ = [
    "BleObservation",
    "Criterion",
    "Database",
    "DetectionConfig",
    "DeviceTrack",
    "GnssConfig",
    "GnssMonitor",
    "GnssStatus",
    "GnssVerdict",
    "LiveDevice",
    "LiveState",
    "SIGNATURES",
    "SafeZone",
    "ThreatLevel",
    "TrackerSignature",
    "TrackingAnalyzer",
    "Verdict",
    "Weights",
    "build_scenario",
    "build_tracks",
    "cluster_positions",
    "haversine",
    "identify",
    "identify_track",
    "link_identities",
    "path_length",
    "proximity_band",
    "render_html",
    "render_hunt",
    "render_json",
    "render_picker",
    "render_text",
    "summarise",
    "trend_label",
]
