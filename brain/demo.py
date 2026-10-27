"""Ketchup brain from the command line.

    python brain/demo.py "let's catch up Thursday"            # agents negotiate, books
    python brain/demo.py "let's catch up Thursday" --pick 2   # manual override
    python brain/demo.py "let's catch up Thursday" --dry-run  # don't book

Every run saves an agent trace to traces/.
"""
import argparse

from gcal import PEOPLE
from pipeline import run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phrase")
    ap.add_argument("--pick", type=int, choices=[1, 2, 3], help="manual override, skips the agents")
    ap.add_argument("--proposer", default="mayo", choices=list(PEOPLE),
                    help="who said the phrase (their agent picks first)")
    ap.add_argument("--dry-run", action="store_true", help="negotiate but don't book")
    args = ap.parse_args()
    run(args.phrase, args.proposer, args.pick, args.dry_run)


if __name__ == "__main__":
    main()
