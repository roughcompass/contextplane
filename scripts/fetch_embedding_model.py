"""Stage the embedding model artifact and verify it against a pinned manifest.

Run at image-build time so the running container never needs network access to
embed anything:

    python scripts/fetch_embedding_model.py --out /opt/models/all-MiniLM-L6-v2

Every file is checked against the sha256 list in
``registry/embedding/model_manifest.json``. A mismatch is fatal and the file is
removed — a partially-written or substituted artifact must not be left behind
looking valid.

``--source`` decides where the bytes come from, which is the point of the script
existing at all rather than being a `curl` in the Dockerfile. Deployments that
cannot reach the public hub stage from whatever channel they do have:

    # default: the upstream hub
    python scripts/fetch_embedding_model.py --out DIR

    # an internal mirror — Artifactory, Nexus, an S3 website, anything HTTP
    python scripts/fetch_embedding_model.py --out DIR --source https://artifacts.corp/minilm

    # a directory: a mounted share, or files carried in on physical media
    python scripts/fetch_embedding_model.py --out DIR --source /mnt/approved/minilm

    # check an artifact staged earlier, downloading nothing
    python scripts/fetch_embedding_model.py --out DIR --verify-only

The source layout must mirror the manifest paths (``onnx/model.onnx``,
``tokenizer.json``, …). ``--with-torch-weights`` additionally stages
``model.safetensors``, which only the sentence_transformers provider and the
parity test need; the container image does not include it.

Exit line: ``staged N file(s) to <dir>`` — or a non-zero exit with the failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent
_MANIFEST = _REPO_ROOT / "registry" / "embedding" / "model_manifest.json"

# Large enough that hashing 90 MB is not dominated by loop overhead, small
# enough not to matter in a build container.
_CHUNK_BYTES = 1024 * 1024


def _load_manifest() -> dict[str, Any]:
    with _MANIFEST.open(encoding="utf-8") as handle:
        manifest: dict[str, Any] = json.load(handle)
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, expected_sha: str, expected_size: int) -> None:
    """Raise unless *path* matches the manifest exactly."""
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(f"{path.name}: expected {expected_size} bytes, got {actual_size}")
    actual_sha = _sha256(path)
    if actual_sha != expected_sha:
        raise ValueError(f"{path.name}: sha256 mismatch\n  expected {expected_sha}\n  actual   {actual_sha}")


def _fetch(source: str, relative: str, destination: Path) -> None:
    """Copy one file from *source* to *destination*, creating parent dirs."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_path = Path(source)
    if source_path.is_dir():
        origin = source_path / relative
        if not origin.is_file():
            raise FileNotFoundError(f"{origin} not found in --source directory")
        shutil.copyfile(origin, destination)
        return

    url = f"{source.rstrip('/')}/{relative}"
    try:
        with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"could not fetch {url}: {exc}. In a network-isolated environment, stage the "
            f"artifact from an internal mirror or a local directory with --source."
        ) from exc


def _stage_one(entry: dict[str, Any], out_dir: Path, source: str, verify_only: bool) -> bool:
    """Stage and verify one manifest entry. Returns True if it was downloaded."""
    relative = str(entry["path"])
    expected_sha = str(entry["sha256"])
    expected_size = int(entry["size"])
    destination = out_dir / relative

    if destination.is_file():
        try:
            _verify(destination, expected_sha, expected_size)
        except ValueError:
            if verify_only:
                raise
            # Present but wrong — a truncated earlier run, or a stale file from
            # a different model revision. Re-fetch rather than trust it.
            print(f"  {relative}: present but does not match the manifest, re-fetching", file=sys.stderr)
        else:
            print(f"  {relative}: ok (already staged)")
            return False

    if verify_only:
        raise FileNotFoundError(f"{destination} not found (--verify-only downloads nothing)")

    print(f"  {relative}: fetching")
    _fetch(source, relative, destination)
    try:
        _verify(destination, expected_sha, expected_size)
    except ValueError:
        # Never leave a file that failed verification on disk. The next run, or
        # a later --verify-only, must not find it and assume it is good.
        destination.unlink(missing_ok=True)
        raise
    return True


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage and verify the embedding model artifact.")
    parser.add_argument("--out", required=True, help="Directory to stage the artifact into")
    parser.add_argument(
        "--source",
        default=None,
        help="Base URL or local directory to fetch from (default: the manifest's default_source)",
    )
    parser.add_argument(
        "--with-torch-weights",
        action="store_true",
        help="Also stage model.safetensors, needed only by the sentence_transformers provider",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify an already-staged artifact; download nothing and fail if anything is missing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest = _load_manifest()
    source = args.source or str(manifest["default_source"])
    out_dir = Path(args.out)

    entries: list[dict[str, Any]] = list(manifest["files"])
    if args.with_torch_weights:
        entries.extend(manifest.get("optional_files", []))

    print(f"{'verifying' if args.verify_only else 'staging'} {manifest['model_id']} in {out_dir}")
    downloaded = 0
    for entry in entries:
        try:
            if _stage_one(entry, out_dir, source, args.verify_only):
                downloaded += 1
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print(f"staged {downloaded} file(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
