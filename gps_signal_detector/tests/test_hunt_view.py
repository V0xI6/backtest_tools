"""Tests du rendu du mode traque."""

import unittest

from gps_signal_detector.hunt_view import (
    gauge,
    gauge_ratio,
    render_hunt,
    render_picker,
    sparkline,
)
from gps_signal_detector.live_state import LiveDevice, LiveState
from gps_signal_detector.models import BleObservation

APPLE = 0x004C
ADDRESS = "7A:2B:3C:4D:5E:6F"


def find_my_separated() -> bytes:
    return b"\x12\x19" + bytes([0x10]) + bytes(24)


def device_with(samples, address: str = ADDRESS, **advertisement) -> LiveDevice:
    device = LiveDevice(address=address)
    for timestamp, rssi in samples:
        device.record(
            BleObservation(timestamp=timestamp, address=address, rssi=rssi, **advertisement)
        )
    return device


def text_of(lines) -> str:
    return "\n".join(line.text for line in lines)


class GaugeTest(unittest.TestCase):
    def test_width_is_respected(self):
        self.assertEqual(len(gauge(0.5, 20)), 20)

    def test_empty_and_full(self):
        self.assertEqual(gauge(0.0, 10), "░" * 10)
        self.assertEqual(gauge(1.0, 10), "█" * 10)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(gauge(-3.0, 10), "░" * 10)
        self.assertEqual(gauge(9.0, 10), "█" * 10)

    def test_marker_is_placed_inside_the_bar(self):
        rendered = gauge(0.2, 20, marker_ratio=0.9)
        self.assertIn("┃", rendered)
        self.assertEqual(len(rendered), 20)

    def test_zero_width(self):
        self.assertEqual(gauge(0.5, 0), "")

    def test_ratio_mapping(self):
        self.assertAlmostEqual(gauge_ratio(-100.0), 0.0)
        self.assertAlmostEqual(gauge_ratio(-35.0), 1.0)
        self.assertAlmostEqual(gauge_ratio(-200.0), 0.0)
        self.assertGreater(gauge_ratio(-50.0), gauge_ratio(-80.0))


class SparklineTest(unittest.TestCase):
    def test_length_matches_requested_width(self):
        samples = [(float(i), -70) for i in range(60)]
        self.assertEqual(len(sparkline(samples, 59.0, 60.0, 40)), 40)

    def test_rising_signal_ends_higher_than_it_starts(self):
        samples = [(float(i), -95 + i) for i in range(60)]
        rendered = sparkline(samples, 59.0, 60.0, 20)
        self.assertLess(rendered.index(rendered[0]), rendered.index(rendered[-1]))
        self.assertEqual(rendered[-1], "█")

    def test_gaps_stay_blank(self):
        # Aucune mesure sur la première moitié : on ne comble pas le trou,
        # un silence est une information.
        samples = [(float(30 + i), -70) for i in range(30)]
        rendered = sparkline(samples, 59.0, 60.0, 20)
        self.assertEqual(rendered[0], " ")
        self.assertNotEqual(rendered[-1], " ")

    def test_slow_emitter_draws_a_continuous_curve(self):
        # Cadence d'un AirTag : une trame toutes les deux secondes. Sans
        # prolongation des trous courts, la courbe serait un pointillé.
        samples = [(float(2 * i), -70 + i) for i in range(30)]
        rendered = sparkline(samples, 58.0, 60.0, 60)
        self.assertLessEqual(rendered.count(" "), 6)

    def test_real_silence_stays_visible(self):
        # Vingt secondes sans la moindre trame : le trou doit se voir.
        samples = [(float(i), -70) for i in range(20)] + [
            (float(40 + i), -70) for i in range(20)
        ]
        rendered = sparkline(samples, 59.0, 60.0, 60)
        self.assertGreater(rendered.count(" "), 8)

    def test_no_samples(self):
        self.assertEqual(sparkline([], 10.0, 60.0, 12), " " * 12)

    def test_zero_width(self):
        self.assertEqual(sparkline([(0.0, -70)], 0.0, 60.0, 0), "")


class HuntScreenTest(unittest.TestCase):
    def _device(self, rssi: int = -55) -> LiveDevice:
        return device_with(
            [(float(i), rssi) for i in range(8)],
            manufacturer_data={APPLE: find_my_separated()},
        )

    def test_shows_identity_and_measurement(self):
        rendered = text_of(render_hunt(self._device(), 7.0))
        self.assertIn("TRAQUE", rendered)
        self.assertIn("Apple Find My / AirTag", rendered)
        self.assertIn(ADDRESS, rendered)
        self.assertIn("séparé du propriétaire", rendered)
        self.assertIn("-55 dBm", rendered)
        self.assertIn("TRÈS PROCHE", rendered)

    def test_warns_that_distance_is_an_order_of_magnitude(self):
        # Le rendu ne doit jamais laisser croire à une mesure de distance.
        rendered = text_of(render_hunt(self._device(), 7.0))
        self.assertIn("ordre de grandeur", rendered)

    def test_approaching_shows_the_hot_indicator(self):
        samples = [(float(i), -85) for i in range(5)] + [
            (float(5 + i), -55) for i in range(5)
        ]
        rendered = render_hunt(device_with(samples), 9.0)
        self.assertIn("VOUS CHAUFFEZ", text_of(rendered))
        self.assertTrue(any(line.style == "hot" for line in rendered))

    def test_moving_away_shows_the_cold_indicator(self):
        samples = [(float(i), -55) for i in range(5)] + [
            (float(5 + i), -88) for i in range(5)
        ]
        rendered = render_hunt(device_with(samples), 9.0)
        self.assertIn("refroidissez", text_of(rendered))

    def test_silent_device_says_so(self):
        rendered = text_of(render_hunt(self._device(), 500.0))
        self.assertIn("Aucune mesure", rendered)

    def test_marks_are_listed_with_the_strongest_flagged(self):
        device = self._device()
        device.mark(7.0)
        device.samples.append((7.5, -40))
        device.mark(7.5)
        rendered = text_of(render_hunt(device, 7.5))
        self.assertIn("Points relevés", rendered)
        self.assertIn("←", rendered)

    def test_deduplication_warning(self):
        rendered = text_of(render_hunt(self._device(), 7.0, samples_per_device=1.0))
        self.assertIn("déduplique", rendered)
        self.assertIn("--restart-scan", rendered)

    def test_no_warning_when_stream_is_healthy(self):
        rendered = text_of(render_hunt(self._device(), 7.0, samples_per_device=8.0))
        self.assertNotIn("déduplique", rendered)

    def test_narrow_terminal_does_not_crash(self):
        for width in (20, 40, 200):
            with self.subTest(width=width):
                self.assertTrue(render_hunt(self._device(), 7.0, width=width))


class PickerScreenTest(unittest.TestCase):
    def _state(self) -> LiveState:
        state = LiveState()
        for index in range(6):
            state.record(
                BleObservation(
                    timestamp=float(index),
                    address="AA:00:00:00:00:01",
                    rssi=-50,
                    manufacturer_data={APPLE: find_my_separated()},
                )
            )
            state.record(
                BleObservation(
                    timestamp=float(index),
                    address="BB:00:00:00:00:02",
                    rssi=-85,
                    name="Enceinte",
                )
            )
        return state

    def test_lists_devices_strongest_first(self):
        lines = render_picker(self._state(), 5.0)
        rendered = text_of(lines)
        self.assertIn("Apple Find My / AirTag", rendered)
        self.assertIn("Enceinte", rendered)
        self.assertLess(
            rendered.index("Apple Find My / AirTag"), rendered.index("Enceinte")
        )

    def test_selection_is_marked(self):
        lines = render_picker(self._state(), 5.0, selected=1)
        selected = [line for line in lines if line.style == "selected"]
        self.assertEqual(len(selected), 1)
        self.assertIn("Enceinte", selected[0].text)

    def test_tracker_filter(self):
        rendered = text_of(render_picker(self._state(), 5.0, trackers_only=True))
        self.assertIn("Apple Find My / AirTag", rendered)
        self.assertNotIn("Enceinte", rendered)

    def test_empty_state_is_explained(self):
        rendered = text_of(render_picker(LiveState(), 0.0))
        self.assertIn("Aucun appareil entendu", rendered)

    def test_counts_identified_trackers(self):
        self.assertIn("1 traceurs identifiés", text_of(render_picker(self._state(), 5.0)))


if __name__ == "__main__":
    unittest.main()
