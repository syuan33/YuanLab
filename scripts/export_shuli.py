# -*- coding: utf-8 -*-
"""Export the SHULI table to static/shuli.json for the blog calculator."""
import json
import os

from shuli import SHULI

OUT = os.path.join(os.path.dirname(__file__), "..", "static", "shuli.json")


def main():
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(SHULI, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


if __name__ == "__main__":
    main()
