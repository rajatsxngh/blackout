"""Blackout: Claude agents vs a rules script vs an optimization solver in a simulated city blackout.

A research sandbox for studying AI coordination, not emergency-response software.
The plan lives in docs/PRD.md.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("blackout")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
