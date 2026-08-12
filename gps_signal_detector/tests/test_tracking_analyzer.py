"""Tests du moteur de décision."""

import unittest

from gps_signal_detector.geo import offset
from gps_signal_detector.models import BleObservation, SafeZone, ThreatLevel
from gps_signal_detector.simulator import HOME, build_scenario
from gps_signal_detector.tracking_analyzer import (
    DetectionConfig,
    TrackingAnalyzer,
    build_tracks,
    can_link,
    guess_address_type,
    infer_scan_cycles,
    link_identities,
)

APPLE = 0x004C
T0 = 1_700_000_000.0


def find_my_separated() -> bytes:
    return b"\x12\x19" + bytes([0x10]) + bytes(24)


def make_observations(
    address: str,
    count: int,
    start: float = T0,
    step: float = 30.0,
    metres_per_step: float = 0.0,
    rssi: int = -62,
    with_position: bool = True,
    **advertisement,
) -> list[BleObservation]:
    """Fabrique une série d'observations régulières, éventuellement mobiles."""
    observations = []
    for index in range(count):
        position = offset(HOME, metres_per_step * index, 0) if with_position else None
        observations.append(
            BleObservation(
                timestamp=start + index * step,
                address=address,
                rssi=rssi,
                latitude=position[0] if position else None,
                longitude=position[1] if position else None,
                **advertisement,
            )
        )
    return observations


def criterion(verdict, name):
    return next(item for item in verdict.criteria if item.name == name)


class AddressTypeTest(unittest.TestCase):
    def test_resolvable_private_address(self):
        self.assertEqual(guess_address_type("7A:BB:CC:DD:EE:FF"), "privée résoluble")

    def test_static_random_address(self):
        self.assertEqual(guess_address_type("C3:BB:CC:DD:EE:FF"), "aléatoire statique")

    def test_malformed_address(self):
        self.assertEqual(guess_address_type("pas-une-adresse"), "inconnue")


class ScanCycleTest(unittest.TestCase):
    def test_groups_close_timestamps(self):
        observations = (
            make_observations("AA:00:00:00:00:01", 3, step=30.0)
            + make_observations("AA:00:00:00:00:02", 3, start=T0 + 1.0, step=30.0)
        )
        # Deux appareils vus aux mêmes trois passages = trois cycles.
        self.assertEqual(len(infer_scan_cycles(observations, tolerance_s=5.0)), 3)


class LinkIdentitiesTest(unittest.TestCase):
    """Un traceur qui change d'adresse ne doit pas se diluer en inconnus."""

    def _airtag(self, address, start, count=30, metres_per_step=400.0):
        return make_observations(
            address,
            count,
            start=start,
            metres_per_step=metres_per_step,
            manufacturer_data={APPLE: find_my_separated()},
        )

    def test_sequential_rotations_are_merged(self):
        first = self._airtag("7A:00:00:00:00:01", T0)
        second = make_observations(
            "7B:00:00:00:00:02",
            30,
            start=T0 + 30 * 30.0,
            metres_per_step=400.0,
            manufacturer_data={APPLE: find_my_separated()},
        )
        # La seconde piste reprend là où la première s'arrête.
        offset_index = 30
        for index, observation in enumerate(second):
            position = offset(HOME, 400.0 * (offset_index + index), 0)
            observation.latitude, observation.longitude = position

        linked = link_identities(build_tracks(first + second))
        self.assertEqual(len(linked), 1)
        self.assertEqual(len(linked[0].addresses), 2)
        self.assertEqual(len(linked[0].observations), 60)

    def test_overlapping_windows_are_kept_apart(self):
        # Deux AirTag présents en même temps : ce sont deux objets.
        first = self._airtag("7A:00:00:00:00:01", T0, metres_per_step=0.0)
        second = self._airtag("7B:00:00:00:00:02", T0 + 60.0, metres_per_step=0.0)
        self.assertEqual(len(link_identities(build_tracks(first + second))), 2)

    def test_different_families_are_never_merged(self):
        airtag = self._airtag("7A:00:00:00:00:01", T0, count=5, metres_per_step=0.0)
        tile = make_observations(
            "7B:00:00:00:00:02", 5, start=T0 + 5 * 30.0, service_uuids=("feed",)
        )
        self.assertEqual(len(link_identities(build_tracks(airtag + tile))), 2)

    def test_unknown_devices_are_not_merged_by_default(self):
        first = make_observations("7A:00:00:00:00:01", 5)
        second = make_observations("7B:00:00:00:00:02", 5, start=T0 + 5 * 30.0)
        self.assertEqual(len(link_identities(build_tracks(first + second))), 2)

    def test_rotation_tolerates_the_distance_we_travelled(self):
        # 30 s à 50 km/h = plus de 400 m : une tolérance fixe casserait tout.
        first = self._airtag("7A:00:00:00:00:01", T0, count=3)
        tracks = build_tracks(first)
        second_tracks = build_tracks(
            make_observations(
                "7B:00:00:00:00:02",
                3,
                start=T0 + 3 * 30.0,
                metres_per_step=400.0,
                manufacturer_data={APPLE: find_my_separated()},
            )
        )
        self.assertTrue(can_link(tracks[0], second_tracks[0], DetectionConfig()))


class ScenarioTest(unittest.TestCase):
    """Le scénario simulé sert de vérité terrain."""

    @classmethod
    def setUpClass(cls):
        cls.scenario = build_scenario()
        cls.verdicts = TrackingAnalyzer().analyze(
            cls.scenario.observations, cls.scenario.scan_times, cls.scenario.safe_zones
        )
        cls.by_label = {verdict.label: verdict for verdict in cls.verdicts}

    def test_airtag_is_confirmed_and_ranked_first(self):
        top = self.verdicts[0]
        self.assertEqual(top.label, "Apple Find My / AirTag")
        self.assertEqual(top.level, ThreatLevel.CONFIRMED)

    def test_rotating_addresses_were_recombined(self):
        self.assertGreater(len(self.verdicts[0].track.addresses), 1)

    def test_cellular_tracker_is_flagged(self):
        self.assertGreaterEqual(
            self.by_label["TK905 GPS Tracker"].level, ThreatLevel.LIKELY
        )

    def test_scenery_stays_below_suspect(self):
        for label in ("Enceinte du salon", "Tile de la boutique", "Téléphone croisé"):
            with self.subTest(label=label):
                self.assertLess(self.by_label[label].level, ThreatLevel.SUSPECT)

    def test_every_real_tracker_outranks_every_decoy(self):
        trackers = [v.score for v in self.verdicts
                    if self.scenario.truth.get(v.label) == "traceur"]
        decoys = [v.score for v in self.verdicts
                  if self.scenario.truth.get(v.label) == "décor"]
        self.assertTrue(trackers and decoys)
        self.assertGreater(min(trackers), max(decoys))


class ScoringRulesTest(unittest.TestCase):
    def test_brief_encounter_scores_no_zones(self):
        # Croisée 60 s en roulant : elle traverse des « zones » sans suivre.
        observations = make_observations(
            "C3:00:00:00:00:01", 3, step=30.0, metres_per_step=500.0,
            service_uuids=("feed",),
        )
        verdict = TrackingAnalyzer().analyze(observations)[0]
        self.assertEqual(criterion(verdict, "Zones distinctes").score, 0.0)
        self.assertEqual(criterion(verdict, "Distance").score, 0.0)
        self.assertLess(verdict.level, ThreatLevel.SUSPECT)

    def test_sustained_travel_scores_zones(self):
        observations = make_observations(
            "C3:00:00:00:00:01", 40, step=30.0, metres_per_step=500.0,
            service_uuids=("feed",),
        )
        verdict = TrackingAnalyzer().analyze(observations)[0]
        self.assertGreater(criterion(verdict, "Zones distinctes").score, 0.0)
        self.assertGreaterEqual(verdict.level, ThreatLevel.LIKELY)

    def test_safe_zone_dampens_a_stationary_device(self):
        observations = make_observations("C3:00:00:00:00:01", 40, metres_per_step=0.0)
        analyzer = TrackingAnalyzer()
        without = analyzer.analyze(observations)[0]
        with_zone = analyzer.analyze(
            observations, safe_zones=[SafeZone("Domicile", HOME[0], HOME[1], 200.0)]
        )[0]
        self.assertLess(with_zone.score, without.score)
        self.assertTrue(any("confiance" in note for note in with_zone.notes))

    def test_trusted_device_is_cleared(self):
        observations = make_observations(
            "C3:00:00:00:00:01", 40, metres_per_step=500.0,
            manufacturer_data={APPLE: find_my_separated()},
        )
        verdict = TrackingAnalyzer().analyze(
            observations, trusted=["c3:00:00:00:00:01"]
        )[0]
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(verdict.level, ThreatLevel.NONE)

    def test_too_few_detections_cannot_alarm(self):
        observations = make_observations(
            "C3:00:00:00:00:01", 2, step=600.0, metres_per_step=5000.0,
            manufacturer_data={APPLE: find_my_separated()},
        )
        verdict = TrackingAnalyzer().analyze(observations)[0]
        self.assertLess(verdict.level, ThreatLevel.LOW)
        self.assertTrue(any("Moins de" in note for note in verdict.notes))

    def test_missing_gps_is_reported_as_degraded(self):
        observations = make_observations(
            "C3:00:00:00:00:01", 40, with_position=False,
            manufacturer_data={APPLE: find_my_separated()},
        )
        verdict = TrackingAnalyzer().analyze(observations)[0]
        self.assertTrue(any("dégradée" in note for note in verdict.notes))
        self.assertEqual(criterion(verdict, "Zones distinctes").score, 0.0)

    def test_score_never_exceeds_one_hundred(self):
        for verdict in TrackingAnalyzer().analyze(build_scenario().observations):
            self.assertLessEqual(verdict.score, 100.0)
            self.assertGreaterEqual(verdict.score, 0.0)

    def test_empty_input(self):
        self.assertEqual(TrackingAnalyzer().analyze([]), [])


if __name__ == "__main__":
    unittest.main()
