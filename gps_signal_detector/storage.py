"""Persistance SQLite des observations, zones sûres et appareils de confiance.

La détection a besoin de mémoire : un traceur ne se démasque qu'en
comparant plusieurs trajets, souvent réalisés à des jours d'intervalle.
Tout est stocké dans un simple fichier SQLite, sans dépendance externe.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from .models import BleObservation, SafeZone

DEFAULT_DB_PATH = Path.home() / ".gps_signal_detector" / "observations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts           REAL NOT NULL,
    address      TEXT NOT NULL,
    rssi         INTEGER NOT NULL,
    name         TEXT,
    payload      TEXT NOT NULL,
    latitude     REAL,
    longitude    REAL
);

CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations(ts);

CREATE TABLE IF NOT EXISTS trusted_devices (
    address  TEXT PRIMARY KEY,
    label    TEXT,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS safe_zones (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    label     TEXT NOT NULL,
    latitude  REAL NOT NULL,
    longitude REAL NOT NULL,
    radius_m  REAL NOT NULL
);
"""


class Database:
    """Accès au journal de détection."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # --- Sessions -----------------------------------------------------

    def start_session(self, label: str = "session") -> int:
        cursor = self.connection.execute(
            "INSERT INTO sessions (label, started_at) VALUES (?, ?)",
            (label, time.time()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def sessions(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT s.id, s.label, s.started_at, COUNT(o.id) AS observations
            FROM sessions s LEFT JOIN observations o ON o.session_id = s.id
            GROUP BY s.id ORDER BY s.started_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Observations -------------------------------------------------

    def add_observations(
        self, session_id: int, observations: Iterable[BleObservation]
    ) -> int:
        rows = []
        for observation in observations:
            payload = observation.to_dict()
            # Les colonnes dédiées servent aux requêtes ; le reste (données
            # constructeur, UUID de service) part en JSON.
            for key in ("timestamp", "address", "rssi", "name", "latitude", "longitude"):
                payload.pop(key, None)
            rows.append(
                (
                    session_id,
                    observation.timestamp,
                    observation.address,
                    observation.rssi,
                    observation.name,
                    json.dumps(payload),
                    observation.latitude,
                    observation.longitude,
                )
            )
        self.connection.executemany(
            """
            INSERT INTO observations
                (session_id, ts, address, rssi, name, payload, latitude, longitude)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()
        return len(rows)

    def load_observations(
        self, session_id: int | None = None, since: float | None = None
    ) -> list[BleObservation]:
        query = "SELECT * FROM observations WHERE 1=1"
        parameters: list = []
        if session_id is not None:
            query += " AND session_id = ?"
            parameters.append(session_id)
        if since is not None:
            query += " AND ts >= ?"
            parameters.append(since)
        query += " ORDER BY ts ASC"

        observations = []
        for row in self.connection.execute(query, parameters):
            payload = json.loads(row["payload"])
            payload.update(
                {
                    "timestamp": row["ts"],
                    "address": row["address"],
                    "rssi": row["rssi"],
                    "name": row["name"],
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                }
            )
            observations.append(BleObservation.from_dict(payload))
        return observations

    # --- Appareils de confiance ---------------------------------------

    def trust(self, address: str, label: str = "") -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO trusted_devices (address, label, added_at) "
            "VALUES (?, ?, ?)",
            (address.upper(), label, time.time()),
        )
        self.connection.commit()

    def untrust(self, address: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM trusted_devices WHERE address = ?", (address.upper(),)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def trusted(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT address, label, added_at FROM trusted_devices ORDER BY address"
            )
        ]

    def trusted_addresses(self) -> list[str]:
        return [entry["address"] for entry in self.trusted()]

    # --- Zones de confiance -------------------------------------------

    def add_safe_zone(
        self, label: str, latitude: float, longitude: float, radius_m: float = 200.0
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO safe_zones (label, latitude, longitude, radius_m) "
            "VALUES (?, ?, ?, ?)",
            (label, latitude, longitude, radius_m),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def remove_safe_zone(self, zone_id: int) -> bool:
        cursor = self.connection.execute("DELETE FROM safe_zones WHERE id = ?", (zone_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def safe_zones(self) -> list[SafeZone]:
        return [
            SafeZone(
                label=row["label"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                radius_m=row["radius_m"],
            )
            for row in self.connection.execute(
                "SELECT label, latitude, longitude, radius_m FROM safe_zones ORDER BY id"
            )
        ]

    def safe_zone_rows(self) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id, label, latitude, longitude, radius_m FROM safe_zones ORDER BY id"
            )
        ]


def store_scan(
    database: Database, observations: Sequence[BleObservation], label: str = "scan"
) -> int:
    """Raccourci : crée une session et y enregistre les observations."""
    session_id = database.start_session(label)
    database.add_observations(session_id, observations)
    return session_id
