"""Tests de l'analyse spectrale (la partie qui ne demande pas de matériel)."""

import unittest

from gps_signal_detector.rf_sweep import (
    UPLINK_BANDS,
    RfHit,
    SweepPass,
    analyse_history,
    detect_peaks,
)


def flat_spectrum(bins: int = 200, floor: float = -80.0):
    freqs = [900e6 + index * 1e3 for index in range(bins)]
    return freqs, [floor] * bins


class DetectPeaksTest(unittest.TestCase):
    def test_flat_noise_has_no_peak(self):
        freqs, powers = flat_spectrum()
        self.assertEqual(detect_peaks(freqs, powers, margin_db=12.0), [])

    def test_single_burst_is_found(self):
        freqs, powers = flat_spectrum()
        powers[100] = -50.0
        hits = detect_peaks(freqs, powers, margin_db=12.0, band_name="test")
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].freq_hz, freqs[100])
        self.assertAlmostEqual(hits[0].snr_db, 30.0)
        self.assertEqual(hits[0].band, "test")

    def test_contiguous_bins_form_one_hit(self):
        freqs, powers = flat_spectrum()
        for index in range(100, 110):
            powers[index] = -55.0
        powers[105] = -45.0
        hits = detect_peaks(freqs, powers, margin_db=12.0)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].freq_hz, freqs[105])
        self.assertAlmostEqual(hits[0].bandwidth_hz, 10 * 1e3)

    def test_burst_touching_the_edge(self):
        freqs, powers = flat_spectrum()
        powers[-1] = -40.0
        self.assertEqual(len(detect_peaks(freqs, powers, margin_db=12.0)), 1)

    def test_strong_carrier_does_not_raise_the_floor(self):
        # Le plancher est une médiane : une porteuse énorme ne doit pas
        # masquer les salves plus discrètes.
        freqs, powers = flat_spectrum()
        powers[20] = 20.0
        powers[150] = -60.0
        self.assertEqual(len(detect_peaks(freqs, powers, margin_db=12.0)), 2)

    def test_mismatched_inputs(self):
        self.assertEqual(detect_peaks([], [], 12.0), [])
        self.assertEqual(detect_peaks([1.0, 2.0], [-80.0], 12.0), [])


class HistoryTest(unittest.TestCase):
    def _passes(self, timestamps, freq_hz=901.0e6):
        return [
            SweepPass(timestamp=ts, hits=[RfHit(freq_hz, -50.0, 25.0, band="GSM 900")])
            for ts in timestamps
        ]

    def test_regular_emitter_is_suspicious(self):
        emitters = analyse_history(self._passes([0.0, 60.0, 120.0, 180.0]))
        self.assertEqual(len(emitters), 1)
        self.assertTrue(emitters[0].suspicious)
        self.assertAlmostEqual(emitters[0].median_interval_s, 60.0)
        self.assertEqual(emitters[0].passes, 4)

    def test_erratic_emitter_is_not_suspicious(self):
        emitters = analyse_history(self._passes([0.0, 10.0, 300.0, 310.0]))
        self.assertFalse(emitters[0].suspicious)

    def test_needs_several_passes(self):
        self.assertEqual(analyse_history(self._passes([0.0, 60.0])), [])

    def test_nearby_frequencies_are_grouped(self):
        passes = [
            SweepPass(timestamp=index * 60.0,
                      hits=[RfHit(901.0e6 + index * 10_000, -50.0, 25.0)])
            for index in range(4)
        ]
        # Décalages de 10 kHz : le même émetteur, vu par un tuner qui dérive.
        self.assertEqual(len(analyse_history(passes, bucket_hz=200_000.0)), 1)

    def test_distinct_frequencies_stay_apart(self):
        passes = []
        for index in range(4):
            passes.append(
                SweepPass(
                    timestamp=index * 60.0,
                    hits=[RfHit(901.0e6, -50.0, 25.0), RfHit(1750.0e6, -55.0, 20.0)],
                )
            )
        self.assertEqual(len(analyse_history(passes)), 2)

    def test_suspicious_emitters_are_listed_first(self):
        # Le téléphone du conducteur émet plus fort, mais n'importe quand ;
        # le traceur est discret et régulier. C'est lui qu'il faut voir.
        passes = []
        for timestamp in (0.0, 5.0, 60.0, 120.0, 180.0):
            hits = []
            if timestamp in (0.0, 60.0, 120.0, 180.0):
                hits.append(RfHit(901.0e6, -50.0, 25.0))
            if timestamp in (0.0, 5.0, 180.0):
                hits.append(RfHit(1750.0e6, -30.0, 45.0))
            passes.append(SweepPass(timestamp=timestamp, hits=hits))

        emitters = analyse_history(passes)
        self.assertEqual(len(emitters), 2)
        self.assertTrue(emitters[0].suspicious)
        self.assertAlmostEqual(emitters[0].freq_hz, 901.0e6, delta=1e3)
        self.assertFalse(emitters[1].suspicious)


class BandPlanTest(unittest.TestCase):
    def test_bands_are_uplink_ranges(self):
        for band in UPLINK_BANDS:
            with self.subTest(band=band.name):
                self.assertLess(band.start_hz, band.stop_hz)

    def test_rtlsdr_reach_is_flagged(self):
        by_name = {band.name: band for band in UPLINK_BANDS}
        self.assertTrue(by_name["GSM 900 / LTE B8"].reachable_by_rtlsdr)
        self.assertFalse(by_name["LTE B7 (2600 MHz)"].reachable_by_rtlsdr)
        self.assertIn("hors portée", str(by_name["LTE B7 (2600 MHz)"]))


class PowerSpectrumTest(unittest.TestCase):
    def test_finds_an_injected_tone(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy absent")

        from gps_signal_detector.rf_sweep import power_spectrum

        sample_rate = 2.4e6
        center = 900e6
        offset_hz = 300e3
        time_axis = np.arange(4096) / sample_rate
        samples = np.exp(2j * np.pi * offset_hz * time_axis)

        freqs, powers = power_spectrum(samples, sample_rate, center, bins=1024)
        peak_freq = freqs[max(range(len(powers)), key=powers.__getitem__)]
        self.assertAlmostEqual(peak_freq, center + offset_hz, delta=sample_rate / 1024)


if __name__ == "__main__":
    unittest.main()
