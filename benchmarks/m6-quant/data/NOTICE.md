# Data notices

- `review_tasks.jsonl.gz`: excerpts of the Python standard library (CPython 3.13) with small mutations
  injected by `m6bench/reviewset.py`. CPython is distributed under the PSF License Version 2
  (https://docs.python.org/3/license.html), Copyright (c) Python Software Foundation. The excerpts are
  modified on purpose, so the bugs in them are not bugs in CPython.
- `chain_bundler.json`: sections of the Bundler changelog (bundler 4.0.18, `CHANGELOG.md`), MIT License,
  Copyright (c) Bundler contributors (https://github.com/ruby/rubygems).

Both files are generated and checked in so every arm sees identical inputs. To regenerate:

    python -m m6bench build-review --stdlib /usr/lib/python3.13
    python -m m6bench build-chain --changelog /path/to/bundler-4.0.18/CHANGELOG.md
