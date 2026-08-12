"""Tests de la persistance SQLite."""

import tempfile
import unittest
from pathlib import Path

from gps_signal_detector.models import BleObservation
from gps_signal_detector.storage import Database, store_scan

APPLE = 0x004C


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "test.db"
        self.database = Database(self.path)

    def tearDown(self):
        self.database.close()
        self._directory.cleanup()

    def observation(self, **kwargs) -> BleObservation:
        kwargs.setdefault("timestamp", 1_700_000_000.0)
        kwargs.setdefault("address", "AA:BB:CC:DD:EE:FF")
        kwargs.setdefault("rssi", -62)
        return BleObservation(**kwargs)


class ObservationRoundTripTest(DatabaseTestCase):
    def test_binary_payloads_survive_the_round_trip(self):
        original = self.observation(
            name="AirTag",
            manufacturer_data={APPLE: b"\x12\x19\x10" + bytes(24)},
            service_uuids=("feed",),
            service_data={"feaa": b"\x41\x02\x03"},
            latitude=48.8566,
            longitude=2.3522,
        )
        session_id = self.database.start_session("test")
        self.database.add_observations(session_id, [original])

        (restored,) = self.database.load_observations()
        self.assertEqual(restored.address, original.address)
        self.assertEqual(restored.manufacturer_data, original.manufacturer_data)
        self.assertEqual(restored.service_data, original.service_data)
        self.assertEqual(restored.service_uuids, original.service_uuids)
        self.assertAlmostEqual(restored.latitude, 48.8566)
        self.assertEqual(restored.rssi, -62)

    def test_observations_without_position(self):
        session_id = self.database.start_session("test")
        self.database.add_observations(session_id, [self.observation()])
        (restored,) = self.database.load_observations()
        self.assertIsNone(restored.latitude)
        self.assertIsNone(restored.position)

    def test_filters_by_session_and_time(self):
        first = store_scan(self.database, [self.observation(timestamp=1000.0)], "hier")
        store_scan(self.database, [self.observation(timestamp=2000.0)], "aujourd'hui")

        self.assertEqual(len(self.database.load_observations()), 2)
        self.assertEqual(len(self.database.load_observations(session_id=first)), 1)
        self.assertEqual(len(self.database.load_observations(since=1500.0)), 1)

    def test_results_are_ordered_by_time(self):
        session_id = self.database.start_session("test")
        self.database.add_observations(
            session_id,
            [self.observation(timestamp=stamp) for stamp in (3000.0, 1000.0, 2000.0)],
        )
        stamps = [obs.timestamp for obs in self.database.load_observations()]
        self.assertEqual(stamps, sorted(stamps))

    def test_sessions_report_their_size(self):
        store_scan(self.database, [self.observation(), self.observation()], "trajet")
        (row,) = self.database.sessions()
        self.assertEqual(row["label"], "trajet")
        self.assertEqual(row["observations"], 2)


class TrustedDevicesTest(DatabaseTestCase):
    def test_add_normalises_case(self):
        self.database.trust("aa:bb:cc:dd:ee:ff", "ma montre")
        self.assertEqual(self.database.trusted_addresses(), ["AA:BB:CC:DD:EE:FF"])

    def test_adding_twice_does_not_duplicate(self):
        self.database.trust("AA:BB:CC:DD:EE:FF")
        self.database.trust("AA:BB:CC:DD:EE:FF", "renommé")
        rows = self.database.trusted()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "renommé")

    def test_remove(self):
        self.database.trust("AA:BB:CC:DD:EE:FF")
        self.assertTrue(self.database.untrust("aa:bb:cc:dd:ee:ff"))
        self.assertFalse(self.database.untrust("AA:BB:CC:DD:EE:FF"))
        self.assertEqual(self.database.trusted(), [])


class SafeZonesTest(DatabaseTestCase):
    def test_add_and_list(self):
        self.database.add_safe_zone("Domicile", 48.8566, 2.3522, 150.0)
        (zone,) = self.database.safe_zones()
        self.assertEqual(zone.label, "Domicile")
        self.assertAlmostEqual(zone.radius_m, 150.0)
        self.assertEqual(zone.position, (48.8566, 2.3522))

    def test_remove(self):
        zone_id = self.database.add_safe_zone("Bureau", 48.8, 2.3)
        self.assertTrue(self.database.remove_safe_zone(zone_id))
        self.assertEqual(self.database.safe_zones(), [])
        self.assertFalse(self.database.remove_safe_zone(zone_id))


class PersistenceTest(unittest.TestCase):
    def test_data_survives_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "persist.db"
            with Database(path) as database:
                store_scan(database, [BleObservation(timestamp=1.0, address="AA:BB:CC:DD:EE:FF")])
                database.trust("11:22:33:44:55:66")
            with Database(path) as reopened:
                self.assertEqual(len(reopened.load_observations()), 1)
                self.assertEqual(len(reopened.trusted()), 1)


if __name__ == "__main__":
    unittest.main()
