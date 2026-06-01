"""Spanreed: an inter-agent message bus for local Claude Code sessions."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("spanreed-bus")
except PackageNotFoundError:  # not installed (e.g. running from a bare source tree)
    __version__ = "0.0.0+unknown"
