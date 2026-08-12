"""Tests de reconnaissance des familles de traceurs."""

import unittest

from gps_signal_detector.models import BleObservation, short_uuid
from gps_signal_detector.signatures import identify, identify_track

APPLE = 0x004C


def observation(**kwargs) -> BleObservation:
    kwargs.setdefault("timestamp", 1_700_000_000.0)
    kwargs.setdefault("address", "AA:BB:CC:DD:EE:FF")
    return BleObservation(**kwargs)


def find_my_separated() -> bytes:
    """Trame Find My longue : clé publique complète, objet séparé."""
    return b"\x12\x19" + bytes([0x10]) + bytes(24)


def find_my_nearby() -> bytes:
    """Trame Find My courte : le propriétaire est à côté."""
    return b"\x12\x02" + bytes([0x00, 0x01])


class ShortUuidTest(unittest.TestCase):
    def test_reduces_bluetooth_base_uuid(self):
        self.assertEqual(short_uuid("0000feed-0000-1000-8000-00805f9b34fb"), "feed")

    def test_accepts_short_forms(self):
        self.assertEqual(short_uuid("0xFEED"), "feed")
        self.assertEqual(short_uuid("FEED"), "feed")

    def test_keeps_proprietary_uuid(self):
        proprietary = "12345678-1234-5678-1234-56789abcdef0"
        self.assertEqual(short_uuid(proprietary.upper()), proprietary)


class AppleFindMyTest(unittest.TestCase):
    def test_detects_separated_airtag(self):
        identity = identify(observation(manufacturer_data={APPLE: find_my_separated()}))
        self.assertIsNotNone(identity.signature)
        self.assertEqual(identity.signature.key, "apple_find_my")
        self.assertTrue(identity.separated)
        self.assertEqual(identity.battery, "pleine")

    def test_nearby_frame_is_not_separated(self):
        identity = identify(observation(manufacturer_data={APPLE: find_my_nearby()}))
        self.assertEqual(identity.signature.key, "apple_find_my")
        self.assertFalse(identity.separated)

    def test_medium_battery_status_byte(self):
        payload = b"\x12\x19" + bytes([0x50]) + bytes(24)
        self.assertEqual(identify(observation(manufacturer_data={APPLE: payload})).battery,
                         "moyenne")

    def test_iphone_is_not_a_tracker(self):
        # Type 0x10 « nearby info » : le téléphone de tout le monde.
        identity = identify(observation(manufacturer_data={APPLE: b"\x10\x05\x01\x02\x03\x04\x05"}))
        self.assertIsNone(identity.signature)

    def test_unpaired_accessory_is_low_confidence(self):
        identity = identify(observation(manufacturer_data={APPLE: b"\x07\x19" + bytes(25)}))
        self.assertEqual(identity.signature.key, "apple_proximity_pairing")
        self.assertEqual(identity.signature.confidence, "faible")

    def test_truncated_frame_is_ignored(self):
        # Longueur annoncée 0x19 mais payload tronqué : on ne devine rien.
        identity = identify(observation(manufacturer_data={APPLE: b"\x12\x19\x10\x01"}))
        self.assertEqual(identity.signature.key, "apple_find_my")
        self.assertFalse(identity.separated)


class OtherVendorsTest(unittest.TestCase):
    def test_tile_by_service_uuid(self):
        identity = identify(
            observation(service_uuids=("0000feed-0000-1000-8000-00805f9b34fb",))
        )
        self.assertEqual(identity.signature.key, "tile")

    def test_samsung_smarttag(self):
        self.assertEqual(identify(observation(service_uuids=("fd5a",))).signature.key,
                         "samsung_smarttag")

    def test_google_fmdn_frame(self):
        identity = identify(observation(service_data={"feaa": b"\x41" + bytes(20)}))
        self.assertEqual(identity.signature.key, "google_fmdn")

    def test_plain_eddystone_beacon_is_not_fmdn(self):
        # Trame 0x00 (Eddystone-UID) : une balise de magasin, pas un traceur.
        identity = identify(observation(service_data={"feaa": b"\x00" + bytes(17)}))
        self.assertIsNone(identity.signature)

    def test_cellular_tracker_by_name(self):
        identity = identify(observation(name="TK905 GPS Tracker"))
        self.assertEqual(identity.signature.key, "cellular_gps_tracker")
        self.assertEqual(identity.signature.confidence, "faible")

    def test_unknown_device(self):
        self.assertIsNone(identify(observation(name="Enceinte du salon")).signature)


class IdentifyTrackTest(unittest.TestCase):
    def test_separated_state_is_sticky(self):
        # Un traceur alterne les deux trames : une seule trame longue suffit.
        identity = identify_track(
            [
                observation(manufacturer_data={APPLE: find_my_nearby()}),
                observation(manufacturer_data={APPLE: find_my_separated()}),
                observation(manufacturer_data={APPLE: find_my_nearby()}),
            ]
        )
        self.assertTrue(identity.separated)

    def test_keeps_most_confident_signature(self):
        identity = identify_track(
            [
                observation(name="GPS Tracker"),  # confiance faible
                observation(service_uuids=("feed",)),  # confiance haute
            ]
        )
        self.assertEqual(identity.signature.key, "tile")

    def test_no_signature_at_all(self):
        self.assertIsNone(identify_track([observation(name="Frigo")]).signature)


if __name__ == "__main__":
    unittest.main()
