from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from pathlib import Path

from defusedxml import ElementTree

from crewmeal.config import DEFAULT_MAX_UPLOAD_BYTES
from crewmeal.models import SourceManifest


class InvalidPresentationError(ValueError):
    """Raised when an upload is not a safe, supported PPTX package."""


RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PRESENTATION_NS = (
    "http://schemas.openxmlformats.org/presentationml/2006/main"
)
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"

NS = {
    "a": DRAWING_NS,
    "c": CHART_NS,
    "p": PRESENTATION_NS,
    "r": OFFICE_REL_NS,
    "pr": RELATIONSHIPS_NS,
}


def validate_pptx(
    data: bytes,
    *,
    filename: str,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> None:
    if Path(filename).suffix.lower() != ".pptx":
        raise InvalidPresentationError("Only .pptx files are supported.")
    if not data:
        raise InvalidPresentationError("The uploaded presentation is empty.")
    if len(data) > max_bytes:
        raise InvalidPresentationError(
            f"The presentation exceeds the {max_bytes // (1024 * 1024)} MB limit."
        )
    if not data.startswith(b"PK"):
        raise InvalidPresentationError("The file is not an Open XML ZIP package.")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = set(package.namelist())
    except zipfile.BadZipFile as exc:
        raise InvalidPresentationError("The PPTX ZIP package is corrupt.") from exc

    required_parts = {
        "[Content_Types].xml",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
    }
    missing = required_parts - names
    if missing:
        raise InvalidPresentationError(
            f"The PPTX package is missing required parts: {sorted(missing)}"
        )


def pptx_content_fingerprint(data: bytes) -> str:
    validate_pptx(data, filename="presentation.pptx")
    digest = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        part_names = sorted(
            name
            for name in package.namelist()
            if name.startswith("ppt/") and not name.endswith("/")
        )
        for name in part_names:
            encoded_name = name.encode("utf-8")
            digest.update(len(encoded_name).to_bytes(4, "big"))
            digest.update(encoded_name)
            digest.update(hashlib.sha256(package.read(name)).digest())
    return f"pptx-sha256:{digest.hexdigest()}"


def build_source_manifest(
    source: Path | bytes,
    *,
    filename: str | None = None,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> SourceManifest:
    if isinstance(source, Path):
        data = source.read_bytes()
        effective_filename = filename or source.name
    else:
        data = source
        effective_filename = filename or "presentation.pptx"

    validate_pptx(data, filename=effective_filename, max_bytes=max_bytes)

    with zipfile.ZipFile(io.BytesIO(data)) as package:
        slide_parts = _ordered_slide_parts(package)
        texts_by_slide: dict[int, tuple[str, ...]] = {}
        links_by_slide: dict[int, tuple[str, ...]] = {}
        alt_text_by_slide: dict[int, tuple[str, ...]] = {}
        notes_by_slide: dict[int, tuple[str, ...]] = {}
        element_counts_by_slide: dict[int, dict[str, int]] = {}

        for slide_number, slide_part in enumerate(slide_parts, start=1):
            root = _read_xml(package, slide_part)
            relationships = _read_relationships(package, slide_part)

            texts_by_slide[slide_number] = (
                _text_values(root)
                + _chart_text_values(package, slide_part, relationships)
            )
            links_by_slide[slide_number] = tuple(
                relationship["target"]
                for relationship in relationships.values()
                if relationship["type"].endswith("/hyperlink")
                and relationship["target_mode"] == "External"
            )
            alt_text_by_slide[slide_number] = _alt_text_values(root)
            notes_by_slide[slide_number] = _notes_values(
                package, slide_part, relationships
            )
            element_counts_by_slide[slide_number] = _element_counts(root)

    return SourceManifest(
        slide_count=len(slide_parts),
        texts_by_slide=texts_by_slide,
        links_by_slide=links_by_slide,
        alt_text_by_slide=alt_text_by_slide,
        notes_by_slide=notes_by_slide,
        element_counts_by_slide=element_counts_by_slide,
    )


def _ordered_slide_parts(package: zipfile.ZipFile) -> list[str]:
    presentation = _read_xml(package, "ppt/presentation.xml")
    relationships = _read_relationships(package, "ppt/presentation.xml")
    slide_parts: list[str] = []
    relationship_attribute = f"{{{OFFICE_REL_NS}}}id"

    for slide_id in presentation.findall("p:sldIdLst/p:sldId", NS):
        relationship_id = slide_id.attrib.get(relationship_attribute)
        if not relationship_id or relationship_id not in relationships:
            raise InvalidPresentationError(
                "The presentation contains an unresolved slide relationship."
            )
        relationship = relationships[relationship_id]
        if not relationship["type"].endswith("/slide"):
            raise InvalidPresentationError(
                "A presentation slide relationship has an unexpected type."
            )
        slide_parts.append(
            _resolve_part("ppt/presentation.xml", relationship["target"])
        )

    if not slide_parts:
        raise InvalidPresentationError("The presentation contains no slides.")
    return slide_parts


def _notes_values(
    package: zipfile.ZipFile,
    slide_part: str,
    relationships: dict[str, dict[str, str]],
) -> tuple[str, ...]:
    notes_relationship = next(
        (
            relationship
            for relationship in relationships.values()
            if relationship["type"].endswith("/notesSlide")
        ),
        None,
    )
    if notes_relationship is None:
        return ()

    notes_part = _resolve_part(slide_part, notes_relationship["target"])
    if notes_part not in package.namelist():
        raise InvalidPresentationError(
            f"The slide references a missing notes part: {notes_part}"
        )
    return _text_values(_read_xml(package, notes_part))


def _chart_text_values(
    package: zipfile.ZipFile,
    slide_part: str,
    relationships: dict[str, dict[str, str]],
) -> tuple[str, ...]:
    values: list[str] = []
    names = set(package.namelist())
    for relationship in relationships.values():
        if (
            not relationship["type"].endswith("/chart")
            or relationship["target_mode"] != "Internal"
        ):
            continue
        chart_part = _resolve_part(slide_part, relationship["target"])
        if chart_part not in names:
            raise InvalidPresentationError(
                f"The slide references a missing chart part: {chart_part}"
            )
        chart = _read_xml(package, chart_part)
        values.extend(_text_values(chart))
        values.extend(
            text
            for element in chart.findall(".//c:v", NS)
            if (text := (element.text or "").strip())
        )
    return tuple(values)


def _text_values(root: ElementTree.Element) -> tuple[str, ...]:
    return tuple(
        text
        for element in root.findall(".//a:t", NS)
        if (text := (element.text or "").strip())
    )


def _alt_text_values(root: ElementTree.Element) -> tuple[str, ...]:
    values: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "cNvPr":
            continue
        for attribute in ("descr", "title"):
            value = element.attrib.get(attribute, "").strip()
            if value and value not in values:
                values.append(value)
    return tuple(values)


def _element_counts(root: ElementTree.Element) -> dict[str, int]:
    names = [_local_name(element.tag) for element in root.iter()]
    return {
        "shapes": names.count("sp"),
        "connectors": names.count("cxnSp"),
        "pictures": names.count("pic"),
        "graphic_frames": names.count("graphicFrame"),
        "tables": names.count("tbl"),
        "charts": names.count("chart"),
    }


def _read_relationships(
    package: zipfile.ZipFile, source_part: str
) -> dict[str, dict[str, str]]:
    relationships_part = _relationships_part(source_part)
    if relationships_part not in package.namelist():
        return {}

    root = _read_xml(package, relationships_part)
    relationships: dict[str, dict[str, str]] = {}
    for relationship in root.findall("pr:Relationship", NS):
        relationship_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        relationship_type = relationship.attrib.get("Type")
        if relationship_id and target and relationship_type:
            relationships[relationship_id] = {
                "target": target,
                "type": relationship_type,
                "target_mode": relationship.attrib.get("TargetMode", "Internal"),
            }
    return relationships


def _read_xml(
    package: zipfile.ZipFile, part_name: str
) -> ElementTree.Element:
    try:
        data = package.read(part_name)
    except KeyError as exc:
        raise InvalidPresentationError(
            f"The PPTX package is missing {part_name}."
        ) from exc
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise InvalidPresentationError(
            f"The PPTX package contains invalid XML in {part_name}."
        ) from exc


def _relationships_part(source_part: str) -> str:
    directory, filename = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _resolve_part(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target).lstrip("/")
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(source_part), target)
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
