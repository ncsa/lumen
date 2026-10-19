"""`monitor` command line: `monitor migrate`, `monitor collect`, `monitor serve` (loops + dashboard)."""

import argparse
import logging
import os
import sys

import requests
from werkzeug.serving import make_server

from monitor import migrate
from monitor.collect import collect_prices
from monitor.config import DEFAULT_CONFIG_PATH, ConfigError, ConfigStore
from monitor.db import make_engine, make_session_factory
from monitor.probe import LiveSnapshot, lumen_state, run_probe
from monitor.scheduler import Scheduler, make_jobs, serve
from monitor.web import create_app

log = logging.getLogger("monitor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monitor", description="Lumen monitoring service")
    parser.add_argument("--config", default=os.environ.get("MONITOR_CONFIG", DEFAULT_CONFIG_PATH), help="path to config.yaml (env MONITOR_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser("migrate", help="upgrade the database schema")
    mig.add_argument("revision", nargs="?", default="head", help="target revision (default: head)")
    col = sub.add_parser("collect", help="run a collector once")
    col.add_argument("--probe", action="store_true", help="probe GET /v1/models once and record the result")
    col.add_argument("--price", action="store_true", help="record today's Lumen, models.dev and hardware prices")
    srv = sub.add_parser("serve", help="run the probe, models and price loops and the dashboard")
    srv.add_argument("--host", default="0.0.0.0", help="dashboard address (default: 0.0.0.0)")
    srv.add_argument("--port", type=int, default=5000, help="dashboard port (default: 5000)")
    args = parser.parse_args(argv)
    if args.command == "collect" and not (args.probe or args.price):
        parser.error("collect: choose a collector, --probe and/or --price")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        store = ConfigStore(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    config = store.current

    if args.command == "migrate":
        migrate.upgrade(config.database_url, args.revision)
        return 0
    if args.command == "collect":
        engine = make_engine(config.database_url)
        session_factory = make_session_factory(engine)
        status = 0
        try:
            # The probe goes first so the price run's hardware cost sees its counters.
            if args.probe:
                result = run_probe(store, session_factory)
                with session_factory() as session:
                    state = lumen_state(session)
                log.info("probe %s in %.0f ms, Lumen is %s", "ok" if result.success else "failed", result.response_time_ms, state)
                status = 0 if result.success else 1
            if args.price:
                try:
                    collect_prices(store, session_factory)
                except (requests.RequestException, ValueError) as exc:
                    log.error("price run failed: %s", exc)
                    status = 1
        finally:
            engine.dispose()
        return status
    engine = make_engine(config.database_url)
    try:
        session_factory = make_session_factory(engine)
        snapshot = LiveSnapshot()
        server = make_server(args.host, args.port, create_app(store, session_factory, snapshot), threaded=True)
        stopped = serve(Scheduler(make_jobs(store, session_factory, snapshot)), server)
    finally:
        engine.dispose()
    return 0 if stopped else 1


if __name__ == "__main__":
    sys.exit(main())
