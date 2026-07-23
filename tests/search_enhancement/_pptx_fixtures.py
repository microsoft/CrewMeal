"""Shared PPTX OOXML fixture builders for the search-enhancement tests.

The ``tests`` tree has no ``__init__.py`` packages, so this module is imported by
bare name (pytest's prepend import mode puts the test directory on ``sys.path``).
The underscore prefix keeps pytest from collecting it as a test module.
"""

from __future__ import annotations

import io
import zipfile

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
SLIDE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)


def _paragraphs(texts: tuple[str, ...]) -> str:
    return "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in texts)


def placeholder(ph_type: str, *texts: str, name: str = "ph") -> str:
    ph = f'<p:ph type="{ph_type}"/>' if ph_type else "<p:ph/>"
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="0" name="{name}"/>'
        f"<p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>"
        f"<p:spPr/><p:txBody><a:bodyPr/>{_paragraphs(texts)}</p:txBody></p:sp>"
    )


def textbox(*texts: str, name: str = "TextBox") -> str:
    """A free-floating text box (no placeholder), which is treated as visual."""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="0" name="{name}"/>'
        f"<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        f"<p:spPr/><p:txBody><a:bodyPr/>{_paragraphs(texts)}</p:txBody></p:sp>"
    )


def picture(name: str = "Picture") -> str:
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="0" name="{name}"/>'
        f"<p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill/><p:spPr/></p:pic>"
    )


def graphic_frame() -> str:
    return "<p:graphicFrame><p:nvGraphicFramePr/><a:graphic/></p:graphicFrame>"


def connector() -> str:
    return "<p:cxnSp><p:nvCxnSpPr/><p:spPr/></p:cxnSp>"


def group(inner: str = "") -> str:
    return f"<p:grpSp><p:nvGrpSpPr/><p:grpSpPr/>{inner}</p:grpSp>"


def _slide_xml(shapes: str) -> str:
    return (
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        f"<p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>"
        f"{shapes}</p:spTree></p:cSld></p:sld>"
    )


def build_pptx(*slides_shapes: str) -> bytes:
    """Build a minimal valid PPTX package from per-slide spTree inner XML."""
    output = io.BytesIO()
    count = len(slides_shapes)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as pkg:
        overrides = "".join(
            f'<Override PartName="/ppt/slides/slide{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'presentationml.slide+xml"/>'
            for i in range(1, count + 1)
        )
        pkg.writestr(
            "[Content_Types].xml",
            f'<Types xmlns="{CT_NS}">'
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.presentationml.presentation.'
            'main+xml"/>'
            f"{overrides}</Types>",
        )
        sld_ids = "".join(
            f'<p:sldId id="{255 + i}" r:id="rId{i}"/>'
            for i in range(1, count + 1)
        )
        pkg.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">'
            f"<p:sldIdLst>{sld_ids}</p:sldIdLst></p:presentation>",
        )
        rels = "".join(
            f'<Relationship Id="rId{i}" Type="{SLIDE_REL}" '
            f'Target="slides/slide{i}.xml"/>'
            for i in range(1, count + 1)
        )
        pkg.writestr(
            "ppt/_rels/presentation.xml.rels",
            f'<Relationships xmlns="{PR_NS}">{rels}</Relationships>',
        )
        for i, shapes in enumerate(slides_shapes, start=1):
            pkg.writestr(f"ppt/slides/slide{i}.xml", _slide_xml(shapes))
    return output.getvalue()
