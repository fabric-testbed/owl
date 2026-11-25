"""OWL: one-way latency measurement using kernel and NIC packet timestamps."""

__version__ = "2.0.0.dev0"

try:  # written into the archive by scripts/build_pyz.py
    from ._build import COMMIT as __commit__
except ImportError:
    __commit__ = None


def version_string() -> str:
    return f"{__version__} (git {__commit__})" if __commit__ else __version__
