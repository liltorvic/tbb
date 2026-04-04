"""
bootstrap.py

Helpers for entry scripts that should work with a repository-local virtualenv
even when launched via the system Python.
"""

from __future__ import annotations

import glob
import os
import sys


def ensure_local_venv_packages() -> None:
    """
    Add `./venv` site-packages to sys.path when running outside the virtualenv.

    This keeps `python main.py` working from the repo root in setups where the
    dependencies were installed into the checked-in local virtualenv.
    """
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return

    root = os.path.dirname(os.path.abspath(__file__))
    pattern = os.path.join(
        root,
        "venv",
        "lib",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
        "site-packages",
    )

    for site_packages in glob.glob(pattern):
        if os.path.isdir(site_packages) and site_packages not in sys.path:
            sys.path.insert(0, site_packages)
