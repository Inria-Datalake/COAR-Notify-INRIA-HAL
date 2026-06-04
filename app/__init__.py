"""COAR Notify INRIA HAL application package.

Exposes ``__version__``, resolved from the installed package metadata so that
``pyproject.toml`` (and the ``[tool.bumpversion]`` workflow) remains the single
source of truth for the version number.
"""

from importlib.metadata import PackageNotFoundError, version


def _resolve_fallback_version() -> str:
    """Return a version string when package metadata is unavailable.

    This runs only when the project isn't installed as a package (e.g. running
    straight from a source checkout without ``pip install -e .``), so the
    installed-metadata lookup below failed. It's a genuine design choice:

      - Return a sentinel like "unknown" or "0.0.0+dev" (simple, honest).
      - Parse the version out of pyproject.toml at the repo root (accurate,
        but couples runtime code to the source layout and adds a TOML read).

    We return an honest sentinel so the UI makes clear this isn't a tagged
    release (rather than masquerading as a real version number).
    """
    return "unknown"


try:
    __version__ = version("coar-notify-inria-hal")
except PackageNotFoundError:
    __version__ = _resolve_fallback_version()
