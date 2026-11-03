"""Entry point: one pipeline run. Equivalent to `python -m scripts.run_pipeline`."""

import sys

from scripts.run_pipeline import main

if __name__ == "__main__":
    sys.exit(main())
