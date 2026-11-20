"""python -m repo_engineer: the same command line as the repo-engineer script."""

import sys

from .cli import main

sys.exit(main())
