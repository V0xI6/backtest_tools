"""Tests de l'interface graphique : traduction en langage courant et serveur."""

import json
import unittest
import urllib.error
import urllib.request

from gps_signal_detector.live_state import LiveDevice, LiveState
from gps_signal_detector.models import BleObservation
from gps_signal_detector.web_ui import (
    HISTORY_POINTS,
    describe_device,
    plain_trend,
    serve,
    signal_percent,
    snapshot,
)

APPLE = 0x004C
ADDRESS = "7A:2B:3C:4D:5E:6F"

# Vocabulaire que l'utilisateur ne doit jamais croiser dans l'affichage
# principal : c'est précisément ce qui rendait la version terminal opaque.
JARGON = ("dBm", "RSSI", "dB-Hz", "trame", "MAC", "C/N0")


def find_my_separated() -> bytes:
    return b"\x12\x19" + bytes([0x10]) + bytes(24)


def device_with(samples, address: str = ADDRESS, **advertisement) -> LiveDevice:
    device = LiveDevice(address=address)
    for timestamp, rssi in samples:
        device.record(
            BleObservation(timestamp=timestamp, address=address, rssi=rssi, **advertisement)
        )
    return device


class SignalPercentTest(unittest.TestCase):
    def test_scale_bounds(self):
        self.assertEqual(signal_percent(-100.0), 0)
        self.assertEqual(signal_percent(-35.0), 100)
        self.assertIsNone(signal_percent(None))

    def test_out_of_range_is_clamped(self):
        self.assertEqual(signal_percent(-140.0), 0)
        self.assertEqual(signal_percent(-10.0), 100)

    def test_closer_means_higher(self):
        self.assertGreater(signal_percent(-50.0), signal_percent(-80.0))


class PlainTrendTest(unittest.TestCase):
    def test_wording_is_plain(self):
        self.assertEqual(plain_trend(6.0)["texte"], "Vous vous rapprochez")
        self.assertEqual(plain_trend(-6.0)["texte"], "Vous vous éloignez")
        self.assertEqual(plain_trend(0.0)["sens"], "stable")

    def test_direction_is_carried_by_more_than_colour(self):
        # Le sens sert la couleur, le texte porte le message : jamais l'un
        # sans l'autre.
        for delta in (6.0, 2.0, 0.0, -2.0, -6.0, None):
            with self.subTest(delta=delta):
                result = plain_trend(delta)
                self.assertTrue(result["texte"])
                self.assertIn(result["sens"], ("chaud", "froid", "stable"))

    def test_no_measurement_yet(self):
        self.assertEqual(plain_trend(None)["niveau"], 0)


class DescribeDeviceTest(unittest.TestCase):
    def _tracker(self, rssi: int = -45) -> LiveDevice:
        return device_with(
            [(float(i), rssi) for i in range(8)],
            manufacturer_data={APPLE: find_my_separated()},
        )

    def test_plain_language_only(self):
        described = describe_device(self._tracker(), 7.0)
        for field in ("nom", "type", "proximite", "indice"):
            for word in JARGON:
                with self.subTest(field=field, word=word):
                    self.assertNotIn(word, str(described[field]))
        self.assertNotIn("dBm", described["tendance"]["texte"])

    def test_tracker_is_named_in_everyday_words(self):
        described = describe_device(self._tracker(), 7.0)
        self.assertEqual(described["nom"], "Traceur Apple AirTag")
        self.assertEqual(described["type"], "Réseau « Localiser » d'Apple")
        self.assertTrue(described["traceur"])

    def test_internal_english_names_never_reach_the_screen(self):
        # « Apple Find My » est le nom interne de la signature : il ne doit
        # pas s'afficher à quelqu'un qui fouille son coffre.
        described = describe_device(self._tracker(), 7.0)
        for field in ("nom", "type"):
            self.assertNotIn("Find My", described[field])

    def test_advertised_name_wins_over_the_generic_one(self):
        device = device_with(
            [(float(i), -60) for i in range(4)],
            name="Porte-clés de Léa",
            service_uuids=("feed",),
        )
        self.assertEqual(describe_device(device, 3.0)["nom"], "Porte-clés de Léa")
        self.assertEqual(describe_device(device, 3.0)["type"], "Réseau Tile")

    def test_separated_device_is_explained(self):
        described = describe_device(self._tracker(), 7.0)
        self.assertIn("signale sa position", described["alerte"])

    def test_close_device_is_critical(self):
        self.assertEqual(describe_device(self._tracker(-45), 7.0)["severite"], "critique")

    def test_distant_device_is_calm(self):
        self.assertEqual(describe_device(self._tracker(-88), 7.0)["severite"], "calme")

    def test_only_three_severities_exist(self):
        seen = {
            describe_device(self._tracker(rssi), 7.0)["severite"]
            for rssi in (-40, -55, -70, -85, -99)
        }
        self.assertEqual(seen, {"calme", "attention", "critique"})

    def test_signal_is_a_percentage(self):
        described = describe_device(self._tracker(-45), 7.0)
        self.assertIsInstance(described["signal"], int)
        self.assertGreaterEqual(described["signal"], 0)
        self.assertLessEqual(described["signal"], 100)

    def test_history_has_a_fixed_length(self):
        described = describe_device(self._tracker(), 7.0)
        self.assertEqual(len(described["historique"]), HISTORY_POINTS)
        self.assertTrue(any(value is not None for value in described["historique"]))

    def test_silent_device_says_so_without_crashing(self):
        described = describe_device(self._tracker(), 900.0)
        self.assertIsNone(described["signal"])
        self.assertTrue(described["muet"])
        self.assertEqual(described["proximite"], "Silencieux")

    def test_unknown_device_is_labelled_plainly(self):
        described = describe_device(device_with([(0.0, -70)]), 0.0)
        self.assertEqual(described["type"], "Appareil Bluetooth non identifié")
        self.assertFalse(described["traceur"])

    def test_low_confidence_match_is_flagged(self):
        described = describe_device(
            device_with([(float(i), -60) for i in range(4)], name="TK905 GPS Tracker"), 3.0
        )
        self.assertIn("à confirmer", described["alerte"])

    def test_raw_values_are_kept_in_the_technical_section(self):
        technique = describe_device(self._tracker(), 7.0)["technique"]
        self.assertEqual(technique["identifiant"], ADDRESS)
        self.assertIsNotNone(technique["puissance_dbm"])
        self.assertEqual(technique["batterie"], "pleine")

    def test_marks_are_reported_as_percentages(self):
        device = self._tracker()
        device.mark(7.0)
        described = describe_device(device, 7.0)
        self.assertEqual(described["points"][0]["n"], 1)
        self.assertIsInstance(described["points"][0]["signal"], int)

    def test_best_signal_is_tracked_without_any_display(self):
        # L'interface web n'a pas de boucle de rendu : le repère doit être
        # tenu à jour par la mesure elle-même.
        self.assertIsNotNone(describe_device(self._tracker(), 7.0)["meilleur"])


class SnapshotTest(unittest.TestCase):
    def _state(self) -> LiveState:
        state = LiveState()
        for index in range(8):
            state.record(
                BleObservation(
                    timestamp=float(index) * 0.3,
                    address=ADDRESS,
                    rssi=-45,
                    manufacturer_data={APPLE: find_my_separated()},
                )
            )
            state.record(
                BleObservation(
                    timestamp=float(index) * 0.3,
                    address="BB:00:00:00:00:02",
                    rssi=-85,
                    name="Enceinte",
                )
            )
        return state

    def test_counts_devices_and_trackers(self):
        payload = snapshot(self._state(), 2.4)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["traceurs"], 1)
        self.assertIsNone(payload["avertissement"])

    def test_strongest_first(self):
        payload = snapshot(self._state(), 2.4)
        self.assertEqual(payload["appareils"][0]["id"], ADDRESS)

    def test_deduplication_is_explained_in_plain_words(self):
        state = LiveState()
        for index in range(4):
            state.record(
                BleObservation(timestamp=1.0, address=f"AA:00:00:00:00:0{index}", rssi=-60)
            )
        avertissement = snapshot(state, 2.0)["avertissement"]
        self.assertIsNotNone(avertissement)
        self.assertIn("--restart-scan", avertissement["texte"])
        for word in ("CoreBluetooth", "RSSI", "dBm"):
            self.assertNotIn(word, avertissement["texte"])

    def test_empty_state(self):
        payload = snapshot(LiveState(), 0.0)
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["appareils"], [])

    def test_payload_is_json_serialisable(self):
        json.dumps(snapshot(self._state(), 2.4))


class ServerTest(unittest.TestCase):
    """Le serveur tourne réellement, sur un port libre attribué par l'OS."""

    @classmethod
    def setUpClass(cls):
        import time

        # Horodatage réel : le serveur travaille avec l'heure courante, et le
        # marquage refuse une mesure trop ancienne.
        now = time.time()
        cls.state = LiveState()
        for index in range(8):
            cls.state.record(
                BleObservation(
                    timestamp=now - index * 0.3,
                    address=ADDRESS,
                    rssi=-45,
                    manufacturer_data={APPLE: find_my_separated()},
                )
            )
        cls.httpd = serve(cls.state, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read()

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_serves_a_self_contained_page(self):
        status, body = self._get("/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Détecteur de traceurs", page)
        # Aucune ressource externe : la page doit fonctionner hors ligne.
        self.assertNotIn("http://", page.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("cdn", page.lower())

    def test_state_endpoint(self):
        status, body = self._get("/api/etat")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["appareils"][0]["id"], ADDRESS)

    def test_mark_endpoint(self):
        status, payload = self._post("/api/marquer", {"id": ADDRESS})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["signal"], int)

    def test_reset_endpoint(self):
        status, payload = self._post("/api/reinitialiser", {"id": ADDRESS})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/marquer", {"id": "00:00:00:00:00:00"})
        self.assertEqual(caught.exception.code, 404)

    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/inexistant")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
