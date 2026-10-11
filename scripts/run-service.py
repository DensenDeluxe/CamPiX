"""Portable background entry point with bounded, private operational logs."""

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    # Installed startup uses its explicit configuration, independent of login-shell overrides.
    for key in tuple(os.environ):
        if key.startswith("CAMPIX_"):
            del os.environ[key]
    # pythonw has no console streams. Native libraries still expect writable streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    args.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(args.log, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    sys.argv = ["campixd", "--config", str(args.config), "serve"]
    from campix.cli import main as serve

    try:
        serve()
    except BaseException as exc:
        logging.getLogger("campix.service").error(
            "CamPiX background service stopped (%s)", type(exc).__name__
        )
        raise


if __name__ == "__main__":
    main()
