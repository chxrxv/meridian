"""Run provenance: hashes of the corpus and golden set, git state."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _norm_bytes(path: Path) -> bytes:
    """File bytes with CRLF normalised, so hashes match across Windows/Linux checkouts."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def is_fixture(path: Path) -> bool:
    raw = path.read_text(encoding="utf-8")
    meta = yaml.safe_load(raw.split("---")[1])
    return bool(meta.get("test_fixture"))


def corpus_files(data_dir: Path, exclude_fixtures: bool = False) -> list[Path]:
    files = sorted((data_dir / "corpus").glob("*.md"))
    return [f for f in files if not (exclude_fixtures and is_fixture(f))]


def corpus_manifest(data_dir: Path, exclude_fixtures: bool = False) -> dict:
    """sha256 over (doc id, sha256 of content) pairs plus the directory file."""
    entries = {
        f.stem: hashlib.sha256(_norm_bytes(f)).hexdigest()
        for f in corpus_files(data_dir, exclude_fixtures)
    }
    entries["__directory__"] = hashlib.sha256(_norm_bytes(data_dir / "directory.json")).hexdigest()
    digest = hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(entries.items())).encode()
    ).hexdigest()
    fixtures = [f.stem for f in corpus_files(data_dir) if is_fixture(f)]
    return {
        "sha256": digest,
        "n_docs": len(entries) - 1,
        "fixtures_included": [] if exclude_fixtures else fixtures,
        "fixtures_excluded": fixtures if exclude_fixtures else [],
    }


def golden_sha256(path: Path) -> str:
    return hashlib.sha256(_norm_bytes(path)).hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - not a git checkout / git missing
        return ""


def git_info() -> dict:
    status = _git("status", "--porcelain", "--", ".", ":!evals/runs")
    return {"commit": _git("rev-parse", "HEAD") or None, "dirty": bool(status)}
