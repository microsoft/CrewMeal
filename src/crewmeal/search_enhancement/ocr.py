"""Local OCR engine for the low (no-Vision) analysis tier.

The low tier reads embedded raster images (screenshots, photographed slides,
diagrams exported as PNG/JPEG) with a CPU-only OCR engine instead of the Vision
LLM. This module is a thin, defensive wrapper around ``rapidocr_onnxruntime``:

* It loads lazily and caches the engine per (model, keys) pair -- construction
  costs ~1s, so the worker reuses one engine across every slide and document.
* Every dependency is optional. If ``rapidocr_onnxruntime`` (or Pillow/numpy)
  is not installed, the engine reports ``available = False`` and returns no text
  rather than crashing the pipeline -- low-tier text extraction still runs.
* The engine bundles a Chinese/English recognition model that cannot read
  Korean. Callers pass a Korean recognition model + character dictionary
  (``rec_model_path`` / ``rec_keys_path``) to recover Hangul; see
  :func:`crewmeal.config.resolve_ocr_model_paths`.
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Images smaller than this on either side are decorative (icons, bullets, rules)
# and never carry OCR-worthy text; skipping them avoids wasted engine calls.
_MIN_OCR_DIMENSION = 40


class OcrEngine:
    """Recognize text in raster image bytes, degrading to a no-op when unavailable."""

    def __init__(
        self,
        *,
        rec_model_path: Path | None = None,
        rec_keys_path: Path | None = None,
    ) -> None:
        self._rec_model_path = rec_model_path
        self._rec_keys_path = rec_keys_path
        self._engine = None
        self._load_failed = False
        self._lock = threading.Lock()

    @property
    def uses_korean_model(self) -> bool:
        return self._rec_model_path is not None

    def _ensure_engine(self):
        if self._engine is not None or self._load_failed:
            return self._engine
        with self._lock:
            if self._engine is not None or self._load_failed:
                return self._engine
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception as exc:  # noqa: BLE001 - optional dependency
                logger.warning(
                    "Low-tier OCR is unavailable (rapidocr_onnxruntime not "
                    "importable: %s); embedded images will not be read.",
                    exc,
                )
                self._load_failed = True
                return None
            kwargs: dict[str, str] = {}
            if self._rec_model_path is not None:
                kwargs["rec_model_path"] = str(self._rec_model_path)
            if self._rec_keys_path is not None:
                kwargs["rec_keys_path"] = str(self._rec_keys_path)
            try:
                self._engine = RapidOCR(**kwargs)
            except Exception as exc:  # noqa: BLE001 - model load may fail
                logger.warning(
                    "Low-tier OCR engine failed to initialize (%s); embedded "
                    "images will not be read.",
                    exc,
                )
                self._load_failed = True
                return None
        return self._engine

    @property
    def available(self) -> bool:
        return self._ensure_engine() is not None

    def read_image(self, blob: bytes) -> tuple[str, ...]:
        """Return recognized text lines for ``blob``; empty when nothing is read."""

        engine = self._ensure_engine()
        if engine is None or not blob:
            return ()
        array = _decode_to_array(blob)
        if array is None:
            return ()
        try:
            result, _elapsed = engine(array)
        except Exception as exc:  # noqa: BLE001 - never fail the pipeline on OCR
            logger.debug("OCR call failed on one image: %s", exc)
            return ()
        if not result:
            return ()
        lines: list[str] = []
        for entry in result:
            # RapidOCR yields ``[box, text, score]`` per detected line.
            if len(entry) < 2:
                continue
            text = str(entry[1]).strip()
            if text:
                lines.append(text)
        return tuple(lines)


def _decode_to_array(blob: bytes):
    """Decode image bytes to an RGB numpy array, or ``None`` if not a raster."""

    try:
        import numpy as np
        from PIL import Image
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        image = Image.open(io.BytesIO(blob))
        image.load()
    except Exception:  # noqa: BLE001 - vector/unsupported/corrupt image
        return None
    if image.width < _MIN_OCR_DIMENSION or image.height < _MIN_OCR_DIMENSION:
        return None
    try:
        return np.array(image.convert("RGB"))
    except Exception:  # noqa: BLE001
        return None


_ENGINE_CACHE: dict[tuple[str | None, str | None], OcrEngine] = {}
_CACHE_LOCK = threading.Lock()


def build_ocr_engine(
    *,
    rec_model_path: Path | None = None,
    rec_keys_path: Path | None = None,
) -> OcrEngine:
    """Return a process-cached :class:`OcrEngine` for the given model paths."""

    key = (
        str(rec_model_path) if rec_model_path else None,
        str(rec_keys_path) if rec_keys_path else None,
    )
    with _CACHE_LOCK:
        engine = _ENGINE_CACHE.get(key)
        if engine is None:
            engine = OcrEngine(
                rec_model_path=rec_model_path, rec_keys_path=rec_keys_path
            )
            _ENGINE_CACHE[key] = engine
        return engine
