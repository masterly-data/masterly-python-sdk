"""`python -m masterly.cli` — the same entry point as the `masterly` console script."""

from __future__ import annotations

import sys

from masterly.cli import main

sys.exit(main())
