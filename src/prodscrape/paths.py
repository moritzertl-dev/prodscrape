"""Where prodscrape reads and writes.

Output paths used to be relative to the working directory, which is fine when you stand
in the repo and wrong everywhere else. Installed via ``uvx`` and launched by Claude
Desktop there is no project directory at all, and ``runs/`` would land wherever Desktop
happened to start.

Layout:

    $PRODSCRAPE_HOME/            default: platform user-data dir, or ./ inside a checkout
      runs/<domain>/             run artifacts
      cache/<domain>/            content-addressed HTTP cache
      recipes/<domain>.yaml      user recipes; these take precedence

Recipes bundled with the package (``prodscrape/recipes/``) ship as defaults and are
read-only. A user recipe of the same name overrides the bundled one, so an install can be
corrected without touching the package.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

ENV_HOME = "PRODSCRAPE_HOME"
BUNDLED_RECIPE_DIR = Path(__file__).resolve().parent / "recipes"


def _is_checkout(path: Path) -> bool:
    """Whether a directory looks like a prodscrape source checkout."""
    return (path / "pyproject.toml").exists() and (path / "src" / "prodscrape").is_dir()


def home() -> Path:
    """The writable root.

    ``PRODSCRAPE_HOME`` wins. Otherwise, working inside a checkout keeps artifacts in the
    checkout (so the repo behaves as it always has), and anywhere else falls back to the
    platform user-data directory.
    """
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()

    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if _is_checkout(candidate):
            return candidate
    return Path(user_data_dir("prodscrape", appauthor=False))


def runs_dir() -> Path:
    return home() / "runs"


def cache_dir() -> Path:
    return home() / "cache"


def user_recipe_dir() -> Path:
    return home() / "recipes"


def recipe_search_path() -> list[Path]:
    """Directories to look in, highest precedence first."""
    return [user_recipe_dir(), BUNDLED_RECIPE_DIR]


def describe() -> dict:
    """Resolved locations, for diagnostics."""
    return {
        "home": str(home()),
        "home_source": (
            f"${ENV_HOME}" if os.environ.get(ENV_HOME)
            else "source checkout" if home() != Path(user_data_dir("prodscrape", appauthor=False))
            else "platform user-data dir"
        ),
        "runs": str(runs_dir()),
        "cache": str(cache_dir()),
        "user_recipes": str(user_recipe_dir()),
        "bundled_recipes": str(BUNDLED_RECIPE_DIR),
    }
