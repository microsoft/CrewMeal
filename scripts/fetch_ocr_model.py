"""Fetch and stage the low-tier OCR recognition model + character dictionary.

CrewMeal's low (no-Vision) analysis tier reads embedded raster images with a
CPU-only OCR engine (``rapidocr_onnxruntime``). That engine bundles a
Chinese/English recognition model that **cannot read Korean**, so Korean decks
need a Korean recognition model (an ONNX ``rec`` model) plus its character
dictionary. Those two files are **not** committed to this repository (the model
is ~13 MB); they are fetched at build/deploy time by this script and can be
pinned by SHA-256.

The engine then discovers them via ``PPTX_OCR_MODEL_DIR`` (a directory holding
``rec.onnx`` + ``dict.txt``) or the explicit ``PPTX_OCR_REC_MODEL`` /
``PPTX_OCR_REC_KEYS`` paths; see :func:`crewmeal.config.resolve_ocr_model_paths`.

Usage (values may also come from the environment)::

    python scripts/fetch_ocr_model.py \
        --rec-url  <url to rec .onnx> --rec-sha256  <hex> \
        --keys-url <url to dict.txt> --keys-sha256 <hex> \
        --dest /opt/ocr/korean

Environment fallbacks:
    PPTX_OCR_REC_URL       URL for the recognition model (.onnx)
    PPTX_OCR_KEYS_URL      URL for the character dictionary (dict.txt)
    PPTX_OCR_REC_SHA256    expected SHA-256 of the .onnx (pins the artifact)
    PPTX_OCR_KEYS_SHA256   expected SHA-256 of the dict.txt
    PPTX_OCR_MODEL_DIR     destination directory (writes rec.onnx + dict.txt)

A known public source of Korean PaddleOCR ONNX weights (Apache-2.0) is the
HuggingFace repo ``monkt/paddleocr-onnx`` under ``languages/korean/``:

    --rec-url  https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages/korean/rec.onnx
    --keys-url https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages/korean/dict.txt

.. important::

   OCR models are third-party assets under their own licenses (PaddleOCR weights
   are Apache-2.0). By running this script you download third-party-licensed
   files; review and accept those terms for your deployment. This script grants
   no license.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import requests

DOWNLOAD_TIMEOUT_SECONDS = 300

LICENSE_NOTICE = (
    "NOTE: OCR models are third-party assets under their own licenses "
    "(PaddleOCR weights are Apache-2.0). Review and accept the model's license "
    "for your deployment. This script grants no license."
)


class FetchError(RuntimeError):
    """A recoverable failure while fetching or staging an OCR asset."""


def download(url: str) -> bytes:
    print(f"Downloading OCR asset from {url} ...", file=sys.stderr)
    response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise FetchError(f"Download failed: HTTP {response.status_code} for {url}")
    return response.content


def verify_checksum(
    data: bytes, expected: str | None, *, label: str, allow_unverified: bool
) -> None:
    digest = hashlib.sha256(data).hexdigest()
    if not expected:
        if allow_unverified:
            print(
                f"WARNING: no checksum for {label}; skipping integrity check. "
                f"Computed sha256={digest}",
                file=sys.stderr,
            )
            return
        raise FetchError(
            f"No expected SHA-256 for {label}. Pass its --*-sha256 to pin the "
            f"artifact, or pass --allow-unverified to bypass. Computed "
            f"sha256={digest}"
        )
    if digest.lower() != expected.strip().lower():
        raise FetchError(
            f"Checksum mismatch for {label}: expected {expected}, computed "
            f"{digest}. Refusing to stage an unverified artifact."
        )
    print(f"Checksum OK for {label} (sha256={digest}).", file=sys.stderr)


def stage(
    url: str,
    sha256: str | None,
    dest: Path,
    filename: str,
    *,
    allow_unverified: bool,
) -> Path:
    data = download(url)
    verify_checksum(
        data, sha256, label=filename, allow_unverified=allow_unverified
    )
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / filename
    target.write_bytes(data)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--rec-url",
        default=os.getenv("PPTX_OCR_REC_URL", "").strip() or None,
        help="URL for the recognition model (.onnx).",
    )
    parser.add_argument(
        "--keys-url",
        default=os.getenv("PPTX_OCR_KEYS_URL", "").strip() or None,
        help="URL for the character dictionary (dict.txt).",
    )
    parser.add_argument(
        "--rec-sha256",
        default=os.getenv("PPTX_OCR_REC_SHA256", "").strip() or None,
        help="Expected SHA-256 of the recognition model.",
    )
    parser.add_argument(
        "--keys-sha256",
        default=os.getenv("PPTX_OCR_KEYS_SHA256", "").strip() or None,
        help="Expected SHA-256 of the character dictionary.",
    )
    parser.add_argument(
        "--dest",
        default=os.getenv("PPTX_OCR_MODEL_DIR", "").strip() or None,
        help="Destination directory (writes rec.onnx + dict.txt).",
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="Proceed without checksums (NOT recommended).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(LICENSE_NOTICE, file=sys.stderr)
    try:
        if not args.dest:
            raise FetchError(
                "No destination directory. Pass --dest or set PPTX_OCR_MODEL_DIR."
            )
        if not args.rec_url or not args.keys_url:
            raise FetchError(
                "Both --rec-url and --keys-url (or PPTX_OCR_REC_URL / "
                "PPTX_OCR_KEYS_URL) are required."
            )
        dest = Path(args.dest)
        rec = stage(
            args.rec_url,
            args.rec_sha256,
            dest,
            "rec.onnx",
            allow_unverified=args.allow_unverified,
        )
        keys = stage(
            args.keys_url,
            args.keys_sha256,
            dest,
            "dict.txt",
            allow_unverified=args.allow_unverified,
        )
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: network failure: {exc}", file=sys.stderr)
        return 1

    print(
        f"Staged low-tier OCR model into {dest}:\n  {rec.name}\n  {keys.name}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
