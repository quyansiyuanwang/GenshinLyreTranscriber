"""Runtime compatibility fixes required by the frozen Windows worker."""

from __future__ import annotations

import os
import shutil
import sys


def install_frozen_replace_fallback() -> None:
    """Fall back to copy/unlink when Numba cache replacement fails on Windows."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    original_replace = os.replace

    def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        try:
            original_replace(source, destination)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {17, 18}:
                raise
            shutil.copyfile(source, destination)
            os.unlink(source)

    os.replace = replace  # type: ignore[assignment]
