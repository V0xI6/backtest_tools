"""Tests des rapports et de la ligne de commande."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from gps_signal_detector.cli import main
from gps_signal_detector.gnss_monitor import GnssMonitor
from gps_signal_detector.report import (
    observer_path,
    render_html,
    render_json,
    render_text,
    summarise,
)
from gps_signal_detector.simulator import build_scenario
from gps_signal_detector.tracking_analyzer import TrackingAnalyzer


class ReportTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = build_scenario()
        cls.verdicts = TrackingAnalyzer().analyze(
            cls.scenario.observations, cls.scenario.scan_times, cls.scenario.safe_zones
        )


class SummaryTest(ReportTestCase):
    def test_counts_and_distance(self):
        stats = summarise(self.verdicts, self.scenario.observations, self.scenario.scan_times)
        self.assertEqual(stats["scans"], len(self.scenario.scan_times))
        self.assertEqual(stats["emetteurs"], len(self.verdicts))
        self.assertGreater(stats["distance_km"], 5.0)
        self.assertEqual(stats["niveau_max"], "confirmé")

    def test_observer_path_has_one_point_per_scan(self):
        path = observer_path(self.scenario.observations)
        self.assertEqual(len(path), len(self.scenario.scan_times))

    def test_summary_of_nothing(self):
        stats = summarise([], [], [])
        self.assertIsNone(stats["debut"])
        self.assertEqual(stats["distance_km"], 0.0)


class TextReportTest(ReportTestCase):
    def test_contains_verdict_and_criteria(self):
        text = render_text(self.verdicts, self.scenario.observations,
                           self.scenario.scan_times, color=False)
        self.assertIn("DÉTECTEUR DE TRACEURS GPS", text)
        self.assertIn("CONFIRMÉ", text)
        self.assertIn("Apple Find My / AirTag", text)
        self.assertIn("Zones distinctes", text)
        self.assertIn("CONDUITE À TENIR", text)

    def test_no_ansi_codes_when_colour_is_off(self):
        text = render_text(self.verdicts, color=False)
        self.assertNotIn("\033[", text)

    def test_colour_can_be_forced(self):
        self.assertIn("\033[", render_text(self.verdicts, color=True))

    def test_limit_shortens_the_report(self):
        short = render_text(self.verdicts, color=False, limit=1)
        self.assertNotIn("TK905", short)

    def test_gnss_section_is_included(self):
        monitor = GnssMonitor()
        monitor.feed("$GPGGA,123519,4807.038,N,01131.000,E,0,00,9.9,545.4,M,46.9,M,,")
        text = render_text(self.verdicts, color=False, gnss=monitor.assess())
        self.assertIn("SIGNAL GNSS", text)

    def test_empty_report(self):
        self.assertIn("Aucun émetteur retenu", render_text([], color=False))


class JsonReportTest(ReportTestCase):
    def test_structure_is_valid_and_complete(self):
        payload = json.loads(
            render_json(self.verdicts, self.scenario.observations, self.scenario.scan_times)
        )
        self.assertEqual(len(payload["emetteurs"]), len(self.verdicts))
        first = payload["emetteurs"][0]
        self.assertEqual(first["niveau"], "confirmé")
        self.assertTrue(first["separe_du_proprietaire"])
        self.assertGreater(len(first["criteres"]), 4)
        self.assertTrue(first["recommandations"])


class HtmlReportTest(ReportTestCase):
    def test_is_self_contained(self):
        html = render_html(self.verdicts, self.scenario.observations)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("Apple Find My / AirTag", html)
        # Aucune ressource externe : le rapport doit s'ouvrir hors ligne.
        self.assertNotIn("http://", html)
        self.assertNotIn("<script", html)

    def test_escapes_device_names(self):
        from gps_signal_detector.models import BleObservation

        observations = [
            BleObservation(timestamp=float(index), address="AA:BB:CC:DD:EE:FF",
                           name="<script>alert(1)</script>")
            for index in range(5)
        ]
        html = render_html(TrackingAnalyzer().analyze(observations))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


class CliTest(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue()

    def test_demo_runs_without_hardware(self):
        code, output = self._run(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("CONFIRMÉ", output)
        self.assertIn("VÉRITÉ TERRAIN", output)

    def test_demo_json_output(self):
        code, output = self._run(["demo", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["resume"]["niveau_max"], "confirmé")

    def test_demo_writes_report_files(self):
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "rapport.html"
            payload = Path(directory) / "rapport.json"
            code, _ = self._run(
                ["demo", "--out-html", str(html), "--out-json", str(payload)]
            )
            self.assertEqual(code, 0)
            self.assertIn("<!doctype html>", html.read_text(encoding="utf-8"))
            self.assertIn("emetteurs", payload.read_text(encoding="utf-8"))

    def test_bands_listing(self):
        code, output = self._run(["bands"])
        self.assertEqual(code, 0)
        self.assertIn("GSM 900", output)

    def test_gnss_replays_a_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "trace.nmea"
            log.write_text(
                "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,\n"
                "$GPGSV,1,1,04,01,30,101,45,02,35,102,45,03,40,103,45,04,45,104,45\n",
                encoding="utf-8",
            )
            code, output = self._run(["gnss", str(log)])
            self.assertEqual(code, 0)
            self.assertIn("Signal GNSS", output)

    def test_trusted_devices_and_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "cli.db")
            self.assertEqual(self._run(["trust", "AA:BB:CC:DD:EE:FF", "--db", db])[0], 0)
            self.assertIn("AA:BB:CC:DD:EE:FF", self._run(["devices", "--db", db])[1])
            self.assertEqual(self._run(["trust", "AA:BB:CC:DD:EE:FF", "--remove",
                                        "--db", db])[0], 0)

            self.assertEqual(
                self._run(["zone", "add", "Domicile", "48.8566", "2.3522", "--db", db])[0], 0
            )
            self.assertIn("Domicile", self._run(["zone", "list", "--db", db])[1])

    def test_analyse_without_data_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _ = self._run(["analyse", "--db", str(Path(directory) / "vide.db")])
            self.assertEqual(code, 1)

    def test_analyse_reads_stored_observations(self):
        from gps_signal_detector.storage import Database, store_scan

        scenario = build_scenario()
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "cli.db")
            with Database(db) as database:
                store_scan(database, scenario.observations, "trajet")
            code, output = self._run(["analyse", "--db", db])
            self.assertEqual(code, 0)
            self.assertIn("CONFIRMÉ", output)

    def test_zone_add_requires_coordinates(self):
        with self.assertRaises(SystemExit):
            self._run(["zone", "add", "Domicile"])


if __name__ == "__main__":
    unittest.main()
