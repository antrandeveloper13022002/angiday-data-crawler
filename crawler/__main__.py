"""python -m crawler <source> ... — one subcommand per source."""

import argparse
import logging
import sys

from crawler.sources import gmaps, grab, tiktok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m crawler", description="Scrape public food-review data into Apify-shaped datasets in data/raw/."
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sources = parser.add_subparsers(dest="source", metavar="<source>", required=True)
    tiktok.register(sources)
    gmaps.register(sources)
    grab.register(sources)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Vietnamese keywords in logs on a Windows console or pipe
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
