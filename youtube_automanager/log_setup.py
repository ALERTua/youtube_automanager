#!/usr/bin/env python3
import logging
import sys


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("youtube_automanager").setLevel(logging.DEBUG if verbose else logging.INFO)
