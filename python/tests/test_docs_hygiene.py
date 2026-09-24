from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PUBLIC_DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / "schemas" / "README.md",
]

FORBIDDEN_PATTERNS = {
    "local task identifier": re.compile(r"(?<![A-Za-z0-9])T\d{2}(?:\.\d+)?(?![A-Za-z0-9])"),
    "hidden local directory path": re.compile(r"(?<![\w.])\.(?!\.)[a-z0-9_-]+[\\/]"),
}


def test_public_documents_do_not_expose_local_task_state() -> None:
    violations: list[str] = []
    for path in PUBLIC_DOCUMENTS:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(line):
                    relative = path.relative_to(ROOT).as_posix()
                    violations.append(f"{relative}:{line_number}: {label}")

    assert not violations, "local task state leaked into public docs:\n" + "\n".join(violations)
