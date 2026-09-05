"""``python -m nwnbot`` — the entry point, per review item ``[r7]``.

The package is deliberately **not installed**: ``pyproject.toml`` stays
pytest-config only, and ``systemd/nwnbot.service`` sets ``WorkingDirectory=``
and ``PYTHONPATH=`` rather than requiring ``pip install -e .`` on the server.
This module is what makes that work.
"""

import sys

from nwnbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
