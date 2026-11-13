"""Run the repeatable backend data jobs: python -m app.jobs."""

import sys

from app.jobs.runner import main


if __name__ == "__main__":
    # Jobs print Ukrainian text and emoji, including when output is redirected
    # on Windows, where the default pipe encoding may otherwise be cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
