"""Enable `python -m firesim ...` as an alias for the pyroSim CLI."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
