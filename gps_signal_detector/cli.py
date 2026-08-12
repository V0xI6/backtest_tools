"""Interface en ligne de commande du détecteur de traceurs GPS."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

from .gnss_monitor import GnssMonitor, GnssVerdict, read_nmea_file
from .models import BleObservation, SafeZone, Verdict
from .report import render_html, render_json, render_text
from .simulator import build_scenario
from .storage import DEFAULT_DB_PATH, Database
from .tracking_analyzer import DetectionConfig, TrackingAnalyzer, infer_scan_cycles


def _write(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")
    print(f"Écrit : {path}", file=sys.stderr)


def _emit(
    args: argparse.Namespace,
    verdicts: Sequence[Verdict],
    observations: Sequence[BleObservation],
    scan_times: Sequence[float],
    gnss: GnssVerdict | None = None,
) -> None:
    """Sortie commune à toutes les commandes d'analyse."""
    if getattr(args, "json", False):
        print(render_json(verdicts, observations, scan_times, gnss))
    else:
        print(
            render_text(
                verdicts,
                observations,
                scan_times,
                gnss=gnss,
                show_all=getattr(args, "all", False),
                limit=getattr(args, "limit", None),
            )
        )
    if getattr(args, "out_json", None):
        _write(args.out_json, render_json(verdicts, observations, scan_times, gnss))
    if getattr(args, "out_html", None):
        _write(args.out_html, render_html(verdicts, observations, scan_times, gnss))


def _analyzer(args: argparse.Namespace) -> TrackingAnalyzer:
    config = DetectionConfig()
    if getattr(args, "min_duration", None):
        config = DetectionConfig(min_duration_s=args.min_duration)
    return TrackingAnalyzer(config)


# --- Commandes --------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    """Analyse un scénario simulé : aucun matériel requis."""
    scenario = build_scenario(seed=args.seed)
    verdicts = _analyzer(args).analyze(
        scenario.observations, scenario.scan_times, scenario.safe_zones
    )
    _emit(args, verdicts, scenario.observations, scenario.scan_times)

    if not args.json:
        print("─" * 78)
        print("VÉRITÉ TERRAIN DU SCÉNARIO (pour vérifier le détecteur)")
        for verdict in verdicts:
            role = scenario.truth.get(verdict.label, "inconnu")
            print(f"  {verdict.level.label:10s} {verdict.label:34s} → {role}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan BLE en direct, puis analyse."""
    from .ble_scanner import ScannerUnavailable, run_monitor
    from .position import build_provider

    provider = None
    try:
        provider = build_provider(args.position)
    except Exception as exc:
        print(f"Source de position inutilisable ({exc}). Analyse dégradée.", file=sys.stderr)

    if provider is None:
        print(
            "Aucune source de position : les critères « zones » et « distance » "
            "resteront à zéro. Voir --position.",
            file=sys.stderr,
        )

    def on_cycle(index: int, batch) -> None:
        position = provider() if provider is not None else None
        location = f" @ {position[0]:.5f},{position[1]:.5f}" if position else ""
        print(f"  scan {index:3d} : {len(batch):3d} appareils{location}", file=sys.stderr)

    print(
        f"Surveillance BLE : {args.cycles or '∞'} cycles de {args.duration:.0f} s "
        f"toutes les {args.interval:.0f} s. Ctrl-C pour arrêter.",
        file=sys.stderr,
    )
    try:
        observations, scan_times = run_monitor(
            cycles=args.cycles,
            interval_s=args.interval,
            duration_s=args.duration,
            position_provider=provider,
            adapter=args.adapter,
            on_cycle=on_cycle,
        )
    except ScannerUnavailable as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    if not observations:
        print("Aucune trame captée.", file=sys.stderr)
        return 1

    safe_zones: list[SafeZone] = []
    trusted: list[str] = []
    if not args.no_db:
        with Database(args.db) as database:
            session_id = database.start_session(args.label)
            database.add_observations(session_id, observations)
            safe_zones = database.safe_zones()
            trusted = database.trusted_addresses()
            if args.history:
                # La session courante vient d'être écrite : la relecture
                # ramène donc l'ancien *et* le nouveau d'un seul coup.
                since = time.time() - args.history * 3600
                observations = database.load_observations(since=since)
                scan_times = infer_scan_cycles(observations)
            print(f"Session {session_id} enregistrée dans {args.db}", file=sys.stderr)

    gnss = None
    if provider is not None and hasattr(provider, "assess"):
        gnss = provider.assess()

    verdicts = _analyzer(args).analyze(observations, scan_times, safe_zones, trusted)
    _emit(args, verdicts, observations, scan_times, gnss)
    return 0


def cmd_traque(args: argparse.Namespace) -> int:
    """Traque temps réel : le signal monte quand on se rapproche."""
    from .ble_scanner import ScannerUnavailable
    from .hunt import run_hunt

    try:
        return run_hunt(
            demo=args.demo,
            address=args.address,
            adapter=args.adapter,
            restart_scan_s=args.restart_scan,
            seed=args.seed,
        )
    except (ScannerUnavailable, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def cmd_analyse(args: argparse.Namespace) -> int:
    """Analyse les observations déjà enregistrées."""
    with Database(args.db) as database:
        since = time.time() - args.since * 3600 if args.since else None
        observations = database.load_observations(session_id=args.session, since=since)
        safe_zones = database.safe_zones()
        trusted = database.trusted_addresses()

    if not observations:
        print("Aucune observation enregistrée. Lancez d'abord « scan ».", file=sys.stderr)
        return 1

    scan_times = infer_scan_cycles(observations)
    verdicts = _analyzer(args).analyze(observations, scan_times, safe_zones, trusted)
    _emit(args, verdicts, observations, scan_times)
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    with Database(args.db) as database:
        rows = database.sessions()
    if not rows:
        print("Aucune session enregistrée.")
        return 1
    print(f"{'id':>4}  {'date':<20} {'trames':>7}  libellé")
    for row in rows:
        started = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(row["started_at"]))
        print(f"{row['id']:>4}  {started:<20} {row['observations']:>7}  {row['label']}")
    return 0


def cmd_gnss(args: argparse.Namespace) -> int:
    """Surveille la santé du signal GNSS (brouillage, leurre)."""
    monitor = GnssMonitor()

    if args.source.startswith("/dev/") or args.source.upper().startswith("COM"):
        from .position import NmeaPositionProvider

        provider = NmeaPositionProvider(args.source, baudrate=args.baudrate)
        provider.start()
        print(f"Écoute de {args.source}… Ctrl-C pour arrêter.", file=sys.stderr)
        try:
            deadline = time.time() + args.seconds if args.seconds else None
            while deadline is None or time.time() < deadline:
                time.sleep(1.0)
                verdict = provider.assess()
                print(f"[{time.strftime('%H:%M:%S')}] {verdict.status.label} — "
                      f"{verdict.reasons[0]}")
        except KeyboardInterrupt:
            pass
        finally:
            provider.stop()
        return 0

    monitor.feed_many(read_nmea_file(args.source))
    verdict = monitor.assess()
    print(f"Signal GNSS : {verdict.status.label}")
    for reason in verdict.reasons:
        print(f"  • {reason}")
    return 0


def cmd_rf(args: argparse.Namespace) -> int:
    """Balaie les bandes montantes à la recherche d'un émetteur périodique."""
    from .rf_sweep import UPLINK_BANDS, SdrSweeper, SdrUnavailable, analyse_history

    bands = [band for band in UPLINK_BANDS if args.all_bands or band.reachable_by_rtlsdr]
    passes = []
    try:
        with SdrSweeper(gain=args.gain) as sweeper:
            for index in range(args.passes):
                sweep = sweeper.sweep(bands, margin_db=args.margin, skip_unreachable=False)
                passes.append(sweep)
                print(
                    f"  passe {index + 1}/{args.passes} : {len(sweep.hits)} pics",
                    file=sys.stderr,
                )
                if index + 1 < args.passes:
                    time.sleep(args.interval)
    except SdrUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2

    emitters = analyse_history(passes)
    if not emitters:
        print("Aucun émetteur récurrent détecté.")
        return 0

    print("Émetteurs récurrents (les périodiques sont les plus suspects) :")
    for emitter in emitters:
        print(f"  {emitter}")
    return 0


def cmd_bands(args: argparse.Namespace) -> int:
    from .rf_sweep import UPLINK_BANDS

    print("Bandes montantes surveillées (ce qu'un traceur émet) :")
    for band in UPLINK_BANDS:
        print(f"  {band}")
        if band.note:
            print(f"      {band.note}")
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    with Database(args.db) as database:
        if args.remove:
            found = database.untrust(args.address)
            print("Retiré." if found else "Adresse absente de la liste.")
            return 0 if found else 1
        database.trust(args.address, args.label or "")
        print(f"{args.address.upper()} marqué comme appareil de confiance.")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    with Database(args.db) as database:
        rows = database.trusted()
    if not rows:
        print("Aucun appareil de confiance déclaré.")
        return 0
    for row in rows:
        print(f"  {row['address']}  {row['label']}")
    return 0


def cmd_zone(args: argparse.Namespace) -> int:
    with Database(args.db) as database:
        if args.action == "add":
            zone_id = database.add_safe_zone(
                args.label, args.latitude, args.longitude, args.radius
            )
            print(f"Zone {zone_id} « {args.label} » enregistrée.")
        elif args.action == "rm":
            found = database.remove_safe_zone(int(args.label))
            print("Supprimée." if found else "Zone introuvable.")
            return 0 if found else 1
        else:
            rows = database.safe_zone_rows()
            if not rows:
                print("Aucune zone de confiance. Ajoutez-en une avec « zone add ».")
                return 0
            for row in rows:
                print(
                    f"  [{row['id']}] {row['label']:<20} "
                    f"{row['latitude']:.5f},{row['longitude']:.5f} "
                    f"rayon {row['radius_m']:.0f} m"
                )
    return 0


# --- Analyse des arguments -------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gps_signal_detector",
        description=(
            "Détecteur de traceurs GPS : repère les balises BLE qui vous "
            "suivent, surveille le brouillage GNSS et balaie les bandes "
            "montantes cellulaires. Usage défensif uniquement."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_output_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--json", action="store_true", help="sortie JSON sur stdout")
        target.add_argument("--out-json", metavar="FICHIER", help="écrit le rapport JSON")
        target.add_argument("--out-html", metavar="FICHIER", help="écrit le rapport HTML")
        target.add_argument("--all", action="store_true", help="affiche aussi les émetteurs anodins")
        target.add_argument("--limit", type=int, help="nombre maximal d'émetteurs affichés")
        target.add_argument(
            "--min-duration",
            type=float,
            metavar="SECONDES",
            help="durée minimale de présence avant de conclure (défaut : 300)",
        )

    def add_db_option(target: argparse.ArgumentParser) -> None:
        target.add_argument("--db", default=str(DEFAULT_DB_PATH), help="base SQLite")

    demo = subparsers.add_parser("demo", help="analyse un scénario simulé (sans matériel)")
    demo.add_argument("--seed", type=int, default=42)
    add_output_options(demo)
    demo.set_defaults(func=cmd_demo)

    scan = subparsers.add_parser("scan", help="scan BLE en direct")
    scan.add_argument("--cycles", type=int, default=0, help="0 = sans fin (Ctrl-C)")
    scan.add_argument("--interval", type=float, default=30.0, help="secondes entre deux scans")
    scan.add_argument("--duration", type=float, default=8.0, help="durée d'écoute par scan")
    scan.add_argument(
        "--position",
        metavar="SOURCE",
        help="« gpsd », « lat,lon », un port série ou un fichier NMEA",
    )
    scan.add_argument("--adapter", help="adaptateur Bluetooth (ex. hci0)")
    scan.add_argument("--label", default="scan", help="libellé de la session")
    scan.add_argument("--no-db", action="store_true", help="ne rien enregistrer")
    scan.add_argument(
        "--history",
        type=float,
        default=0.0,
        metavar="HEURES",
        help="inclut les observations passées : c'est en croisant plusieurs "
        "trajets qu'un traceur se démasque",
    )
    add_db_option(scan)
    add_output_options(scan)
    scan.set_defaults(func=cmd_scan)

    traque = subparsers.add_parser(
        "traque",
        help="traque temps réel dans le terminal : le signal monte quand on approche",
    )
    traque.add_argument(
        "--demo", action="store_true", help="scénario simulé, sans matériel Bluetooth"
    )
    traque.add_argument("--address", help="traquer directement cette adresse")
    traque.add_argument("--adapter", help="adaptateur Bluetooth (ex. hci0)")
    traque.add_argument(
        "--restart-scan",
        type=float,
        default=0.0,
        metavar="SECONDES",
        help="relance le scan périodiquement ; à activer si l'écran signale "
        "que la plateforme déduplique les annonces",
    )
    traque.add_argument("--seed", type=int, default=42)
    traque.set_defaults(func=cmd_traque)

    analyse = subparsers.add_parser("analyse", help="analyse les données enregistrées")
    analyse.add_argument("--session", type=int, help="identifiant de session")
    analyse.add_argument("--since", type=float, metavar="HEURES", help="fenêtre temporelle")
    add_db_option(analyse)
    add_output_options(analyse)
    analyse.set_defaults(func=cmd_analyse)

    sessions = subparsers.add_parser("sessions", help="liste les sessions enregistrées")
    add_db_option(sessions)
    sessions.set_defaults(func=cmd_sessions)

    gnss = subparsers.add_parser("gnss", help="surveille le signal GNSS")
    gnss.add_argument("source", help="fichier NMEA ou port série (/dev/ttyACM0)")
    gnss.add_argument("--baudrate", type=int, default=9600)
    gnss.add_argument("--seconds", type=float, help="durée de surveillance (série)")
    gnss.set_defaults(func=cmd_gnss)

    radio = subparsers.add_parser("rf", help="balayage RF des bandes montantes (RTL-SDR)")
    radio.add_argument("--passes", type=int, default=6, help="nombre de passes")
    radio.add_argument("--interval", type=float, default=60.0, help="secondes entre les passes")
    radio.add_argument("--margin", type=float, default=12.0, help="seuil au-dessus du bruit (dB)")
    radio.add_argument("--gain", default="auto")
    radio.add_argument("--all-bands", action="store_true", help="inclut les bandes hors RTL-SDR")
    radio.set_defaults(func=cmd_rf)

    bands = subparsers.add_parser("bands", help="liste les bandes surveillées")
    bands.set_defaults(func=cmd_bands)

    trust = subparsers.add_parser("trust", help="déclare un appareil de confiance")
    trust.add_argument("address")
    trust.add_argument("--label", help="nom lisible")
    trust.add_argument("--remove", action="store_true", help="retire de la liste")
    add_db_option(trust)
    trust.set_defaults(func=cmd_trust)

    devices = subparsers.add_parser("devices", help="liste les appareils de confiance")
    add_db_option(devices)
    devices.set_defaults(func=cmd_devices)

    zone = subparsers.add_parser("zone", help="gère les zones de confiance (domicile…)")
    zone.add_argument("action", choices=("add", "list", "rm"))
    zone.add_argument("label", nargs="?", help="nom de la zone, ou son identifiant pour « rm »")
    zone.add_argument("latitude", nargs="?", type=float)
    zone.add_argument("longitude", nargs="?", type=float)
    zone.add_argument("--radius", type=float, default=200.0, help="rayon en mètres")
    add_db_option(zone)
    zone.set_defaults(func=cmd_zone)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "zone" and args.action == "add" and (
        args.label is None or args.latitude is None or args.longitude is None
    ):
        parser.error("« zone add » attend : LIBELLÉ LATITUDE LONGITUDE")
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - interaction clavier
        print("\nInterrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
