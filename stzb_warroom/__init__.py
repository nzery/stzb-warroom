"""stzb-warroom (率土战局): report this computer's games to the server and show its notifications."""

try:
    from ._version import __version__  # written by the Windows build from the release's tag
except ImportError:  # run from source: no release
    __version__ = "0.0.0"
