"""Confirm the existing ownership test fails against a disposable source copy."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from dotenv import load_dotenv

root = Path(__file__).resolve().parent
load_dotenv(root / ".env")
with TemporaryDirectory(prefix="phase0-negative-") as temporary:
    copy = Path(temporary)
    for name in ("lab", "accounts", "notes", "habits", "pages", "tests"):
        shutil.copytree(root / name, copy / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(root / "pyproject.toml", copy)
    path = copy / "notes/api.py"
    original = path.read_text(encoding="utf-8")
    correct = "return Note.objects.filter(owner=request.auth)"
    assert original.count(correct) == 1
    path.write_text(original.replace(correct, "return Note.objects.all()"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_contracts.py::test_note_crud_and_ownership"],
        cwd=copy, env={**os.environ, "PHASE0_TEST_DB": "test_phase0_negative"}, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 1 or "assert outsider.get(NOTES).json() == []" not in result.stdout:
        raise SystemExit("Negative control did not fail at the expected ownership assertion.")
    print("Negative control detected the ownership regression.")
