"""`monitor` command line: `monitor migrate`, `monitor serve`."""

import argparse
import logging
import os
import sys

from monitor import migrate
from monitor.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

log = logging.getLogger("monitor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monitor", description="Lumen monitoring service")
    parser.add_argument("--config", default=os.environ.get("MONITOR_CONFIG", DEFAULT_CONFIG_PATH), help="path to config.yaml (env MONITOR_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser("migrate", help="upgrade the database schema")
    mig.add_argument("revision", nargs="?", default="head", help="target revision (default: head)")
    sub.add_parser("serve", help="run the collectors and the dashboard")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.command == "migrate":
        migrate.upgrade(config.database_url, args.revision)
        return 0
    log.error("serve is not implemented yet")
    return 1


if __name__ == "__main__":
    sys.exit(main())
