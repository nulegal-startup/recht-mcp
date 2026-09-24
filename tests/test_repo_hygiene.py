# SPDX-License-Identifier: AGPL-3.0-only
"""Guards on the repository itself: licence headers, and nothing in the tree
that belongs to the private side of the service."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEXT = [p for p in ROOT.rglob("*")
        if p.is_file() and p.suffix in {".py", ".md", ".toml", ".json", ".txt", ""}
        and not any(part in {".git", ".venv", "__pycache__", ".pytest_cache"}
                    for part in p.parts)
        and p.name not in {"LICENSE", "uv.lock", "test_repo_hygiene.py"}]
PY = [p for p in TEXT if p.suffix == ".py"]


@pytest.mark.parametrize("path", PY, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_source_file_carries_the_spdx_header(path):
    assert path.read_text(encoding="utf-8").startswith(
        "# SPDX-License-Identifier: AGPL-3.0-only\n")


def test_the_licence_is_the_unmodified_agpl():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "Version 3, 19 November 2007" in text
    assert len(text) > 30_000


@pytest.mark.parametrize("path", TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_private_detail_in_the_tree(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    # the one address this repository publishes
    emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)) - {"security@nulegal.de"}
    assert not emails, emails
    # issue-tracker keys: a short upper-case/digit key with a letter, a dash,
    # a number (dates and dockets have no letter; a few standard names do)
    keys = set(re.findall(r"\b(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,6}-\d{1,5}\b", text))
    assert not keys - {"AGPL-3", "UTF-8"}, keys
    # SQL: the database stays private
    assert not re.search(r"(?i)\bselect\s+[\w.*, ]+\s+from\s+\w", text)
    assert not re.search(r"(?i)\b(?:insert\s+into|update\s+\w+\s+set|join\s+\w+\s+\w+\s+on)\b",
                         text)
