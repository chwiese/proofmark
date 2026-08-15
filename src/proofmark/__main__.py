"""Allow `python -m proofmark`."""

import sys

from proofmark.cli import main

if __name__ == "__main__":
    sys.exit(main())
