"""Balayage radio des bandes montantes cellulaires (réception seule).

Un traceur GPS pour véhicule ne se contente pas d'écouter les satellites :
il doit *émettre* pour transmettre la position, par salves régulières sur
les bandes montantes GSM/LTE. C'est sa faiblesse. Un boîtier collé sous un
pare-chocs, même sans interface BLE, trahit sa présence par une salve qui
revient toutes les N minutes, toujours à la même fréquence, et qui reste
forte quel que soit l'endroit où l'on se gare.

L'analyse (détection de pics, recherche de périodicité) est en Python pur
et donc testable sans matériel ; seule l'acquisition demande une clé
RTL-SDR et numpy.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# Limite haute typique d'un tuner R820T2 : au-delà, une clé RTL-SDR est
# aveugle et il faut un HackRF ou équivalent.
RTLSDR_MAX_HZ = 1_766_000_000


class SdrUnavailable(RuntimeError):
    """Aucune clé SDR utilisable, ou pilotes manquants."""


@dataclass(frozen=True)
class Band:
    """Bande de fréquences à surveiller."""

    name: str
    start_hz: float
    stop_hz: float
    note: str = ""

    @property
    def reachable_by_rtlsdr(self) -> bool:
        return self.stop_hz <= RTLSDR_MAX_HZ

    def __str__(self) -> str:
        return (
            f"{self.name} : {self.start_hz / 1e6:.1f}–{self.stop_hz / 1e6:.1f} MHz"
            f"{'' if self.reachable_by_rtlsdr else ' (hors portée RTL-SDR)'}"
        )


# Bandes *montantes* : ce que le traceur émet, pas ce que l'antenne-relais
# diffuse. Balayer les descendantes ne montrerait que le réseau.
UPLINK_BANDS: tuple[Band, ...] = (
    Band("LTE B20 (800 MHz)", 832e6, 862e6, "très utilisée par les objets connectés en Europe"),
    Band("GSM 900 / LTE B8", 880e6, 915e6, "traceurs 2G bon marché"),
    Band("DCS 1800 / LTE B3", 1710e6, 1785e6, "limite haute d'une clé RTL-SDR"),
    Band("LTE B1 (2100 MHz)", 1920e6, 1980e6, "demande un SDR à large couverture"),
    Band("LTE B7 (2600 MHz)", 2500e6, 2570e6, "demande un SDR à large couverture"),
    Band("ISM 2,4 GHz", 2400e6, 2483.5e6, "interfaces BLE/Wi-Fi de configuration"),
)


@dataclass(frozen=True)
class RfHit:
    """Pic d'énergie détecté au-dessus du plancher de bruit."""

    freq_hz: float
    power_db: float
    snr_db: float
    bandwidth_hz: float = 0.0
    band: str = ""

    def __str__(self) -> str:
        return (
            f"{self.freq_hz / 1e6:9.3f} MHz  {self.snr_db:5.1f} dB au-dessus du bruit"
            f"  ({self.band})"
        )


@dataclass
class SweepPass:
    """Une passe complète de balayage, horodatée."""

    timestamp: float = field(default_factory=time.time)
    hits: list[RfHit] = field(default_factory=list)


@dataclass(frozen=True)
class PeriodicEmitter:
    """Émetteur revenant régulièrement sur la même fréquence."""

    freq_hz: float
    passes: int
    median_interval_s: float
    regularity: float  # 0 = métronome, 1 = totalement erratique
    peak_snr_db: float
    band: str = ""

    @property
    def suspicious(self) -> bool:
        return self.passes >= 3 and self.regularity < 0.35

    def __str__(self) -> str:
        verdict = "PÉRIODIQUE" if self.suspicious else "irrégulier"
        return (
            f"{self.freq_hz / 1e6:9.3f} MHz  {self.passes} salves  "
            f"toutes les ~{self.median_interval_s / 60:.1f} min  "
            f"({self.peak_snr_db:.0f} dB)  [{verdict}]"
        )


def detect_peaks(
    freqs: Sequence[float],
    powers_db: Sequence[float],
    margin_db: float = 12.0,
    band_name: str = "",
) -> list[RfHit]:
    """Extrait les pics dépassant le plancher de bruit d'au moins `margin_db`.

    Le plancher est pris comme la médiane du spectre : robuste aux quelques
    porteuses fortes qui, avec une moyenne, relèveraient le seuil au point
    de masquer les salves plus faibles.
    """
    if not freqs or len(freqs) != len(powers_db):
        return []

    floor = statistics.median(powers_db)
    threshold = floor + margin_db

    hits: list[RfHit] = []
    start: int | None = None
    for index, power in enumerate(powers_db):
        above = power >= threshold
        if above and start is None:
            start = index
        elif not above and start is not None:
            hits.append(_hit_from_slice(freqs, powers_db, start, index, floor, band_name))
            start = None
    if start is not None:
        hits.append(_hit_from_slice(freqs, powers_db, start, len(powers_db), floor, band_name))
    return hits


def _hit_from_slice(
    freqs: Sequence[float],
    powers_db: Sequence[float],
    start: int,
    stop: int,
    floor: float,
    band_name: str,
) -> RfHit:
    window = list(range(start, stop))
    peak_index = max(window, key=lambda i: powers_db[i])
    bin_width = abs(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    return RfHit(
        freq_hz=freqs[peak_index],
        power_db=powers_db[peak_index],
        snr_db=powers_db[peak_index] - floor,
        bandwidth_hz=bin_width * len(window),
        band=band_name,
    )


def analyse_history(
    passes: Sequence[SweepPass],
    bucket_hz: float = 200_000.0,
    min_passes: int = 3,
) -> list[PeriodicEmitter]:
    """Cherche, entre les passes, les émetteurs qui reviennent en cadence.

    Un téléphone dans la voiture émet quand son propriétaire s'en sert,
    donc n'importe quand. Un traceur, lui, rapporte sa position sur minuterie :
    c'est cette régularité que l'on cherche, pas la puissance.
    """
    buckets: dict[int, list[tuple[float, RfHit]]] = {}
    for sweep in passes:
        for hit in sweep.hits:
            key = int(round(hit.freq_hz / bucket_hz))
            buckets.setdefault(key, []).append((sweep.timestamp, hit))

    emitters: list[PeriodicEmitter] = []
    for key, entries in buckets.items():
        if len(entries) < min_passes:
            continue
        entries.sort(key=lambda item: item[0])
        timestamps = [timestamp for timestamp, _ in entries]
        intervals = [
            later - earlier for earlier, later in zip(timestamps, timestamps[1:]) if later > earlier
        ]
        if not intervals:
            continue
        median_interval = statistics.median(intervals)
        spread = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
        regularity = spread / median_interval if median_interval > 0 else 1.0
        emitters.append(
            PeriodicEmitter(
                freq_hz=statistics.fmean(hit.freq_hz for _, hit in entries),
                passes=len(entries),
                median_interval_s=median_interval,
                regularity=regularity,
                peak_snr_db=max(hit.snr_db for _, hit in entries),
                band=entries[0][1].band,
            )
        )
    return sorted(emitters, key=lambda emitter: (not emitter.suspicious, -emitter.peak_snr_db))


def power_spectrum(samples, sample_rate: float, center_hz: float, bins: int = 1024):
    """Densité spectrale de puissance en dB (moyenne de périodogrammes).

    Nécessite numpy ; le reste du module s'en passe.
    """
    try:
        import numpy as np  # type: ignore
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise SdrUnavailable("L'analyse spectrale demande numpy : pip install numpy") from exc

    data = np.asarray(samples, dtype=np.complex64)
    usable = (len(data) // bins) * bins
    if usable == 0:
        return [], []
    frames = data[:usable].reshape(-1, bins)
    window = np.hanning(bins)
    spectra = np.fft.fftshift(np.fft.fft(frames * window, axis=1), axes=1)
    power = np.mean(np.abs(spectra) ** 2, axis=0) / (bins * np.sum(window**2))
    power_db = 10 * np.log10(power + 1e-20)
    freqs = center_hz + np.fft.fftshift(np.fft.fftfreq(bins, 1 / sample_rate))
    return freqs.tolist(), power_db.tolist()


class SdrSweeper:
    """Pilote une clé RTL-SDR pour balayer les bandes montantes."""

    def __init__(
        self,
        sample_rate: float = 2.4e6,
        gain: str | float = "auto",
        device_index: int = 0,
        bins: int = 1024,
    ) -> None:
        self.sample_rate = sample_rate
        self.gain = gain
        self.device_index = device_index
        self.bins = bins
        self._sdr = None

    def open(self) -> None:  # pragma: no cover - dépend du matériel
        try:
            from rtlsdr import RtlSdr  # type: ignore
        except ImportError as exc:
            raise SdrUnavailable(
                "Le balayage RF demande une clé RTL-SDR et pyrtlsdr :\n"
                "  pip install pyrtlsdr numpy\n"
                "puis les pilotes librtlsdr de votre distribution."
            ) from exc
        try:
            self._sdr = RtlSdr(self.device_index)
        except Exception as exc:
            raise SdrUnavailable(f"Clé SDR inaccessible : {exc}") from exc
        self._sdr.sample_rate = self.sample_rate
        self._sdr.gain = self.gain

    def close(self) -> None:  # pragma: no cover - dépend du matériel
        if self._sdr is not None:
            self._sdr.close()
            self._sdr = None

    def __enter__(self) -> "SdrSweeper":  # pragma: no cover - dépend du matériel
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:  # pragma: no cover - dépend du matériel
        self.close()

    def sweep_band(
        self, band: Band, dwell_samples: int = 262_144, margin_db: float = 12.0
    ) -> list[RfHit]:  # pragma: no cover - dépend du matériel
        """Balaie une bande par pas de fréquence et renvoie les pics."""
        if self._sdr is None:
            raise SdrUnavailable("Appelez open() (ou utilisez le gestionnaire de contexte).")

        # On n'exploite que 80 % de la largeur : les bords d'une capture SDR
        # sont déformés par le filtre d'entrée.
        step = self.sample_rate * 0.8
        hits: list[RfHit] = []
        center = band.start_hz + step / 2
        while center < band.stop_hz:
            self._sdr.center_freq = center
            samples = self._sdr.read_samples(dwell_samples)
            freqs, powers = power_spectrum(samples, self.sample_rate, center, self.bins)
            usable = [
                (freq, power)
                for freq, power in zip(freqs, powers)
                if band.start_hz <= freq <= band.stop_hz
                and abs(freq - center) < step / 2
            ]
            if usable:
                hits.extend(
                    detect_peaks(
                        [freq for freq, _ in usable],
                        [power for _, power in usable],
                        margin_db,
                        band.name,
                    )
                )
            center += step
        return hits

    def sweep(
        self,
        bands: Iterable[Band] = UPLINK_BANDS,
        dwell_samples: int = 262_144,
        margin_db: float = 12.0,
        skip_unreachable: bool = True,
    ) -> SweepPass:  # pragma: no cover - dépend du matériel
        """Une passe complète sur toutes les bandes demandées."""
        sweep_pass = SweepPass()
        for band in bands:
            if skip_unreachable and not band.reachable_by_rtlsdr:
                continue
            sweep_pass.hits.extend(self.sweep_band(band, dwell_samples, margin_db))
        return sweep_pass
