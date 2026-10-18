"""`monitor` command line: `monitor migrate`, `monitor collect`, `monitor serve`."""

import argparse
import logging
import os
import sys

import requests

from monitor import migrate
from monitor.collect import collect_prices
from monitor.config import DEFAULT_CONFIG_PATH, ConfigError, ConfigStore
from monitor.db import make_engine, make_session_factory

log = logging.getLogger("monitor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monitor", description="Lumen monitoring service")
    parser.add_argument("--config", default=os.environ.get("MONITOR_CONFIG", DEFAULT_CONFIG_PATH), help="path to config.yaml (env MONITOR_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser("migrate", help="upgrade the database schema")
    mig.add_argument("revision", nargs="?", default="head", help="target revision (default: head)")
    col = sub.add_parser("collect", help="run a collector once")
    col.add_argument("--price", action="store_true", help="record today's Lumen, models.dev and hardware prices")
    sub.add_parser("serve", help="run the collectors and the dashboard")
    args = parser.parse_args(argv)
    if args.command == "collect" and not args.price:
        parser.error("collect: choose a collector, e.g. --price")

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
        try:
            collect_prices(store, make_session_factory(engine))
        except (requests.RequestException, ValueError) as exc:
            log.error("price run failed: %s", exc)
            return 1
        finally:
            engine.dispose()
        return 0
    log.error("serve is not implemented yet")
    return 1


if __name__ == "__main__":
    sys.exit(main())
