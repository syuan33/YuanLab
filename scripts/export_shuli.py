# -*- coding: utf-8 -*-
"""Export the SHULI table to static/ (JSON plus a copy of the library) for the blog."""
import json
import os
import shutil

from shuli import SHULI

OUT = os.path.join(os.path.dirname(__file__), "..", "static", "shuli.json")


def main():
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(SHULI, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    shutil.copyfile(os.path.join(os.path.dirname(__file__), "shuli.py"),
                    os.path.join(os.path.dirname(OUT), "shuli.py"))


if __name__ == "__main__":
    main()
