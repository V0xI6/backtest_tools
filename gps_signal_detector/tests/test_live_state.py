"""Tests du moteur temps réel : lissage, tendance, classement."""

import unittest

from gps_signal_detector.live_state import (
    LiveDevice,
    LiveState,
    proximity_band,
    trend_label,
)
from gps_signal_detector.models import BleObservation

APPLE = 0x004C
ADDRESS = "7A:2B:3C:4D:5E:6F"


def find_my_separated() -> bytes:
    return b"\x12\x19" + bytes([0x10]) + bytes(24)


def device_with(samples, address: str = ADDRESS, **advertisement) -> LiveDevice:
    """Construit un appareil à partir d'une liste (instant, puissance)."""
    device = LiveDevice(address=address)
    for timestamp, rssi in samples:
        device.record(
            BleObservation(
                timestamp=timestamp, address=address, rssi=rssi, **advertisement
            )
        )
    return device


class SmoothingTest(unittest.TestCase):
    def test_median_of_recent_samples(self):
        device = device_with([(float(i), -70) for i in range(4)] + [(4.0, -20)])
        # La valeur aberrante ne doit pas emporter la jauge.
        self.assertEqual(device.smoothed(4.0), -70)

    def test_ignores_samples_that_are_too_old(self):
        device = device_with([(0.0, -50), (20.0, -80)])
        self.assertEqual(device.smoothed(20.0, max_age_s=12.0), -80)

    def test_no_recent_measurement(self):
        device = device_with([(0.0, -50)])
        self.assertIsNone(device.smoothed(100.0))

    def test_empty_device(self):
        self.assertIsNone(LiveDevice(address=ADDRESS).smoothed(0.0))


class TrendTest(unittest.TestCase):
    def test_approaching_gives_a_positive_trend(self):
        samples = [(float(i), -80) for i in range(5)] + [
            (float(5 + i), -60) for i in range(5)
        ]
        self.assertAlmostEqual(device_with(samples).trend(9.0), 20.0)

    def test_moving_away_gives_a_negative_trend(self):
        samples = [(float(i), -55) for i in range(5)] + [
            (float(5 + i), -85) for i in range(5)
        ]
        self.assertAlmostEqual(device_with(samples).trend(9.0), -30.0)

    def test_needs_two_samples_on_each_side(self):
        # Six mesures : cinq récentes, une seule ancienne — pas assez.
        device = device_with([(float(i), -70) for i in range(6)])
        self.assertIsNone(device.trend(5.0))
        device.record(BleObservation(timestamp=6.0, address=ADDRESS, rssi=-70))
        self.assertIsNotNone(device.trend(6.0))

    def test_stable_signal_has_no_trend(self):
        device = device_with([(float(i), -70) for i in range(10)])
        self.assertAlmostEqual(device.trend(9.0), 0.0)


class LabelsTest(unittest.TestCase):
    def test_proximity_bands(self):
        self.assertEqual(proximity_band(-40)[0], "À PORTÉE DE MAIN")
        self.assertEqual(proximity_band(-60)[0], "TRÈS PROCHE")
        self.assertEqual(proximity_band(-70)[0], "PROCHE")
        self.assertEqual(proximity_band(-85)[0], "À DISTANCE")
        self.assertEqual(proximity_band(-99)[0], "LIMITE DE PORTÉE")

    def test_band_boundaries_are_inclusive(self):
        self.assertEqual(proximity_band(-50.0)[0], "À PORTÉE DE MAIN")
        self.assertEqual(proximity_band(-50.1)[0], "TRÈS PROCHE")

    def test_trend_labels(self):
        self.assertIn("CHAUFFEZ", trend_label(6.0)[0])
        self.assertIn("monte", trend_label(2.0)[0])
        self.assertIn("stable", trend_label(0.0)[0])
        self.assertIn("descend", trend_label(-2.0)[0])
        self.assertIn("refroidissez", trend_label(-8.0)[0])

    def test_trend_label_without_measurement(self):
        self.assertIn("attente", trend_label(None)[0])


class CalibrationTest(unittest.TestCase):
    def test_best_signal_is_remembered(self):
        device = device_with([(float(i), -80) for i in range(5)])
        device.update_best(4.0)
        self.assertEqual(device.best_rssi, -80)

        for index in range(5):
            device.record(
                BleObservation(timestamp=5.0 + index, address=ADDRESS, rssi=-55)
            )
        device.update_best(9.0)
        self.assertEqual(device.best_rssi, -55)

    def test_best_never_goes_down_on_its_own(self):
        device = device_with([(float(i), -55) for i in range(5)])
        device.update_best(4.0)
        for index in range(5):
            device.record(
                BleObservation(timestamp=5.0 + index, address=ADDRESS, rssi=-90)
            )
        device.update_best(9.0)
        self.assertEqual(device.best_rssi, -55)

    def test_reset_clears_calibration_and_marks(self):
        device = device_with([(float(i), -60) for i in range(5)])
        device.update_best(4.0)
        device.mark(4.0)
        device.reset_calibration()
        self.assertIsNone(device.best_rssi)
        self.assertEqual(device.marks, [])

    def test_marks_are_numbered(self):
        device = device_with([(float(i), -60) for i in range(5)])
        first = device.mark(4.0)
        second = device.mark(4.0)
        self.assertEqual((first.index, second.index), (1, 2))
        self.assertEqual(first.rssi, -60)

    def test_mark_without_measurement_returns_nothing(self):
        self.assertIsNone(LiveDevice(address=ADDRESS).mark(0.0))


class IdentificationTest(unittest.TestCase):
    def test_signature_is_detected_from_advertisements(self):
        device = device_with(
            [(0.0, -60)], manufacturer_data={APPLE: find_my_separated()}
        )
        self.assertEqual(device.signature.key, "apple_find_my")
        self.assertTrue(device.separated)
        self.assertEqual(device.battery, "pleine")
        self.assertEqual(device.label, "Apple Find My / AirTag")

    def test_name_takes_precedence_for_the_label(self):
        self.assertEqual(device_with([(0.0, -60)], name="Tile").label, "Tile")

    def test_falls_back_to_the_address(self):
        self.assertEqual(device_with([(0.0, -60)]).label, ADDRESS)


class LiveStateTest(unittest.TestCase):
    def _state(self) -> LiveState:
        state = LiveState()
        for index in range(6):
            state.record(
                BleObservation(timestamp=float(index), address="AA:00:00:00:00:01", rssi=-50)
            )
            state.record(
                BleObservation(timestamp=float(index), address="BB:00:00:00:00:02", rssi=-85)
            )
        return state

    def test_strongest_device_comes_first(self):
        ranked = self._state().ranked(5.0)
        self.assertEqual(ranked[0].address, "AA:00:00:00:00:01")

    def test_silent_devices_are_pushed_to_the_end(self):
        state = self._state()
        # L'appareil fort se tait, le faible continue de répondre.
        for index in range(6):
            state.record(
                BleObservation(timestamp=100.0 + index, address="BB:00:00:00:00:02", rssi=-85)
            )
        ranked = state.ranked(105.0)
        self.assertEqual(ranked[0].address, "BB:00:00:00:00:02")

    def test_filter_keeps_only_known_trackers(self):
        state = self._state()
        state.record(
            BleObservation(
                timestamp=6.0,
                address="CC:00:00:00:00:03",
                rssi=-70,
                manufacturer_data={APPLE: find_my_separated()},
            )
        )
        filtered = state.ranked(6.0, trackers_only=True)
        self.assertEqual([device.address for device in filtered], ["CC:00:00:00:00:03"])

    def test_prune_forgets_long_silent_devices(self):
        state = self._state()
        self.assertEqual(state.prune(10_000.0), 2)
        self.assertEqual(state.devices, {})

    def test_samples_per_device_detects_deduplication(self):
        # Une seule trame par appareil : le symptôme d'une plateforme qui
        # déduplique, et donc d'une traque impossible.
        state = LiveState()
        for index in range(4):
            state.record(
                BleObservation(timestamp=1.0, address=f"AA:00:00:00:00:0{index}", rssi=-60)
            )
        self.assertAlmostEqual(state.samples_per_device(2.0), 1.0)

    def test_samples_per_device_when_healthy(self):
        state = LiveState()
        for index in range(10):
            state.record(
                BleObservation(timestamp=index * 0.2, address="AA:00:00:00:00:01", rssi=-60)
            )
        self.assertGreater(state.samples_per_device(2.0), 1.2)

    def test_counts_total_observations(self):
        self.assertEqual(self._state().total_observations, 12)


if __name__ == "__main__":
    unittest.main()
