"""Tests de l'analyse NMEA et de la détection brouillage / leurre."""

import unittest

from gps_signal_detector.gnss_monitor import (
    GnssMonitor,
    GnssStatus,
    _coordinate,
    parse_sentence,
)


def nmea(body: str) -> str:
    """Ajoute la somme de contrôle attendue à une phrase NMEA."""
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}"


def gga(fix_quality: int = 1, sats: int = 8, hdop: float = 0.9) -> str:
    return nmea(
        f"GPGGA,123519,4807.038,N,01131.000,E,{fix_quality},{sats:02d},{hdop},545.4,M,46.9,M,,"
    )


def gsv(snrs: list[int]) -> list[str]:
    """Construit les phrases GSV décrivant les satellites en vue."""
    sentences = []
    total = max(1, (len(snrs) + 3) // 4)
    for message in range(total):
        chunk = snrs[message * 4 : (message + 1) * 4]
        fields = [f"GPGSV,{total},{message + 1},{len(snrs)}"]
        for index, snr in enumerate(chunk):
            prn = message * 4 + index + 1
            fields.append(f"{prn:02d},{30 + index * 5:02d},{100 + prn:03d},{snr:02d}")
        sentences.append(nmea(",".join(fields)))
    return sentences


class ParsingTest(unittest.TestCase):
    def test_valid_sentence(self):
        parsed = parse_sentence(gga())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "GGA")

    def test_bad_checksum_is_rejected(self):
        self.assertIsNone(parse_sentence("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9*00"))

    def test_garbage_is_rejected(self):
        self.assertIsNone(parse_sentence("bonjour"))
        self.assertIsNone(parse_sentence(""))
        self.assertIsNone(parse_sentence("$GP"))

    def test_sentence_without_checksum_is_accepted(self):
        self.assertIsNotNone(parse_sentence("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9"))

    def test_coordinate_conversion(self):
        self.assertAlmostEqual(_coordinate("4807.038", "N"), 48.1173, places=4)
        self.assertAlmostEqual(_coordinate("4807.038", "S"), -48.1173, places=4)
        self.assertIsNone(_coordinate("", ""))

    def test_any_constellation_is_accepted(self):
        # Galileo, GLONASS, multi-constellation : même grammaire.
        for talker in ("GA", "GL", "GN", "BD"):
            with self.subTest(talker=talker):
                self.assertIsNotNone(parse_sentence(nmea(f"{talker}GSA,A,3,04,05,,,,,,,,,,,2.5,1.3,2.1")))


class SnapshotTest(unittest.TestCase):
    def test_reads_position_satellites_and_dop(self):
        monitor = GnssMonitor()
        monitor.feed(gga(sats=9, hdop=1.2))
        monitor.feed_many(gsv([40, 35, 30, 45, 38, 42]))
        snapshot = monitor.snapshot
        self.assertEqual(snapshot.sats_used, 9)
        self.assertAlmostEqual(snapshot.hdop, 1.2)
        self.assertEqual(len(snapshot.satellites), 6)
        self.assertAlmostEqual(snapshot.mean_cn0, 38.33, places=1)
        self.assertAlmostEqual(snapshot.latitude, 48.1173, places=4)

    def test_speed_from_rmc(self):
        monitor = GnssMonitor()
        monitor.feed(nmea("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"))
        self.assertAlmostEqual(monitor.snapshot.speed_kmh, 41.5, delta=0.2)

    def test_gsa_provides_fix_type(self):
        monitor = GnssMonitor()
        monitor.feed(nmea("GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1"))
        self.assertEqual(monitor.snapshot.fix_type, 3)
        self.assertAlmostEqual(monitor.snapshot.hdop, 1.3)


class HealthTest(unittest.TestCase):
    def test_healthy_signal(self):
        monitor = GnssMonitor()
        monitor.feed(gga())
        monitor.feed_many(gsv([30, 35, 40, 45, 28, 50]))
        self.assertEqual(monitor.assess().status, GnssStatus.OK)

    def test_jamming_when_carriers_collapse(self):
        monitor = GnssMonitor()
        monitor.feed(gga())
        monitor.feed_many(gsv([22, 15, 19, 24, 12, 18]))
        verdict = monitor.assess()
        self.assertEqual(verdict.status, GnssStatus.JAMMING)
        self.assertIn("C/N0", verdict.reasons[0])

    def test_jamming_on_sudden_drop(self):
        monitor = GnssMonitor()
        monitor.feed(gga())
        for _ in range(5):
            monitor.feed_many(gsv([40, 42, 38, 41, 39, 43]))
            monitor.assess()
        # Toujours au-dessus du plancher absolu, mais 14 dB plus bas.
        monitor.feed_many(gsv([26, 27, 25, 28, 24, 26]))
        verdict = monitor.assess()
        self.assertEqual(verdict.status, GnssStatus.JAMMING)
        self.assertTrue(any("Chute" in reason for reason in verdict.reasons))

    def test_jamming_when_fix_is_lost_with_satellites_in_view(self):
        monitor = GnssMonitor()
        monitor.feed(gga(fix_quality=1))
        monitor.feed_many(gsv([40, 35, 30, 45, 38, 42]))
        self.assertEqual(monitor.assess().status, GnssStatus.OK)

        monitor.feed(gga(fix_quality=0))
        verdict = monitor.assess()
        self.assertEqual(verdict.status, GnssStatus.JAMMING)
        self.assertTrue(any("point fixe" in reason for reason in verdict.reasons))

    def test_spoofing_when_signals_are_too_uniform(self):
        monitor = GnssMonitor()
        monitor.feed(gga())
        monitor.feed_many(gsv([45] * 8))
        verdict = monitor.assess()
        self.assertEqual(verdict.status, GnssStatus.SPOOFING)
        self.assertTrue(any("uniformes" in reason for reason in verdict.reasons))

    def test_uniform_but_weak_signals_are_jamming_not_spoofing(self):
        # Un brouilleur écrase toutes les porteuses de la même façon :
        # l'uniformité seule ne suffit pas à crier au leurre.
        monitor = GnssMonitor()
        monitor.feed(gga())
        monitor.feed_many(gsv([18] * 8))
        self.assertEqual(monitor.assess().status, GnssStatus.JAMMING)

    def test_spoofing_on_impossible_position_jump(self):
        monitor = GnssMonitor()
        monitor.feed(gga())
        monitor.feed_many(gsv([30, 35, 40, 45, 28, 50]))
        monitor.assess(elapsed_s=1.0)

        monitor.feed(nmea("GPGGA,123529,4907.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"))
        verdict = monitor.assess(elapsed_s=1.0)
        self.assertEqual(verdict.status, GnssStatus.SPOOFING)
        self.assertTrue(any("km/h" in reason for reason in verdict.reasons))

    def test_no_fix_is_only_degraded(self):
        monitor = GnssMonitor()
        monitor.feed(gga(fix_quality=0, sats=0))
        self.assertEqual(monitor.assess().status, GnssStatus.DEGRADED)

    def test_verdict_is_serialisable(self):
        monitor = GnssMonitor()
        monitor.feed(gga())
        monitor.feed_many(gsv([30, 35, 40, 45, 28, 50]))
        payload = monitor.assess().to_dict()
        self.assertEqual(payload["statut"], "nominal")
        self.assertEqual(payload["satellites_visibles"], 6)


if __name__ == "__main__":
    unittest.main()
