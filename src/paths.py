"""
Month-End Close Assistant - generic path and hash helpers.

Three small functions that several layers need and none of them owns: hashing a
file, slugging a label into a directory name, and rendering a path the way an
artefact records it.

They were originally defined in ``report.py``. Anything wanting one of them had
to import the reporting module, which pulls in pandas, numpy and reportlab - so
the reviewer CLI loaded the whole analytics and PDF stack to print five lines of
text. They live here instead, with no third-party and no project imports, so a
tool can use them without inheriting a dependency it has no use for.

``report.py`` re-exports all three, so ``from report import sha256_of`` keeps
working. There is exactly one implementation of each, and it is this one.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = ["sha256_of", "slugify", "display_path"]


def sha256_of(path: str | Path) -> str:
    """SHA-256 of a file, read in chunks so a large artefact is not held in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(label: str) -> str:
    """Lowercase, non-alphanumerics to '-', so a label can never escape its directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    return slug or "unlabelled"


def display_path(path: str | Path) -> str:
    """Path as shown in the provenance appendix.

    Rendered relative to the working directory when the file sits under it, so
    two callers that name the same file differently (one absolute, one relative)
    produce identical documents - and so a committed example report does not
    carry the author's home directory.
    """
    candidate = Path(path)
    try:
        return Path(candidate).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return candidate.resolve().as_posix()
