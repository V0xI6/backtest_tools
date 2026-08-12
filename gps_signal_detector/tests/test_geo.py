"""Tests des utilitaires géographiques."""

import unittest

from gps_signal_detector.geo import (
    cluster_positions,
    haversine,
    interpolate,
    max_spread,
    offset,
    path_length,
)

PARIS = (48.8566, 2.3522)


class HaversineTest(unittest.TestCase):
    def test_same_point_is_zero(self):
        self.assertAlmostEqual(haversine(PARIS, PARIS), 0.0, places=6)

    def test_known_distance_paris_lyon(self):
        lyon = (45.7640, 4.8357)
        self.assertAlmostEqual(haversine(PARIS, lyon) / 1000, 392, delta=5)

    def test_offset_round_trip(self):
        moved = offset(PARIS, north_m=1000, east_m=0)
        self.assertAlmostEqual(haversine(PARIS, moved), 1000, delta=5)


class PathLengthTest(unittest.TestCase):
    def test_straight_line(self):
        points = [PARIS, offset(PARIS, 500, 0), offset(PARIS, 1000, 0)]
        self.assertAlmostEqual(path_length(points), 1000, delta=10)

    def test_gps_jitter_is_ignored(self):
        # Un récepteur immobile « respire » de quelques mètres : sommer ce
        # bruit ferait croire à un déplacement.
        jitter = [offset(PARIS, north, east) for north, east in
                  [(0, 0), (3, -2), (-4, 1), (2, 3), (-1, -3)]]
        self.assertEqual(path_length(jitter), 0.0)

    def test_single_point(self):
        self.assertEqual(path_length([PARIS]), 0.0)
        self.assertEqual(path_length([]), 0.0)


class ClusterTest(unittest.TestCase):
    def test_nearby_points_form_one_zone(self):
        points = [offset(PARIS, north, 0) for north in (0, 20, 40, 60)]
        self.assertEqual(len(cluster_positions(points, radius_m=150)), 1)

    def test_distant_points_form_several_zones(self):
        points = [PARIS, offset(PARIS, 5000, 0), offset(PARIS, 0, 5000)]
        self.assertEqual(len(cluster_positions(points, radius_m=150)), 3)

    def test_empty_input(self):
        self.assertEqual(cluster_positions([]), [])


class MiscTest(unittest.TestCase):
    def test_max_spread(self):
        points = [PARIS, offset(PARIS, 100, 0), offset(PARIS, 2000, 0)]
        self.assertAlmostEqual(max_spread(points), 2000, delta=10)

    def test_max_spread_needs_two_points(self):
        self.assertEqual(max_spread([PARIS]), 0.0)

    def test_interpolate_reaches_destination(self):
        destination = offset(PARIS, 1000, 0)
        steps = interpolate(PARIS, destination, 4)
        self.assertEqual(len(steps), 4)
        self.assertAlmostEqual(haversine(steps[-1], destination), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
