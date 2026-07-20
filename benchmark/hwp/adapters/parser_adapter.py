from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import json
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def _package_version(name: str) -> str:
    return importlib.metadata.version(name)


def _markdown_to_text(markdown: str) -> str:
    markdown = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", markdown)
    markdown = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", markdown)
    lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"\|?(?:\s*:?-+:?\s*\|)+\s*", line):
            continue
        if line.startswith("|") and line.endswith("|"):
            line = "\t".join(part.strip() for part in line.strip("|").split("|"))
        line = re.sub(r"[`*_>#]", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _markdown_tables(markdown: str) -> list[dict[str, Any]]:
    lines = markdown.splitlines()
    tables: list[dict[str, Any]] = []
    index = 1
    while index < len(lines):
        separator = lines[index].strip()
        if not re.fullmatch(r"\|?(?:\s*:?-+:?\s*\|)+\s*", separator):
            index += 1
            continue
        header = [cell.strip() for cell in lines[index - 1].strip().strip("|").split("|")]
        cells = [header]
        cursor = index + 1
        while cursor < len(lines):
            row = lines[cursor].strip()
            if not row.startswith("|") or not row.endswith("|"):
                break
            cells.append([cell.strip() for cell in row.strip("|").split("|")])
            cursor += 1
        tables.append(
            {
                "rows": len(cells),
                "columns": max((len(row) for row in cells), default=0),
                "cells": cells,
            }
        )
        index = cursor
    return tables


def _table_summary(table: Any) -> dict[str, Any]:
    raw = dataclasses.asdict(table) if dataclasses.is_dataclass(table) else {}
    cells_value = getattr(table, "cells", raw.get("cells"))
    if cells_value is None:
        cells_value = getattr(table, "rows", raw.get("rows", []))
    cells: list[list[str]] = []
    if isinstance(cells_value, list):
        for row in cells_value:
            if not isinstance(row, list):
                continue
            cells.append(
                [
                    str(
                        getattr(cell, "text", cell.get("text", ""))
                        if isinstance(cell, dict)
                        else getattr(cell, "text", cell)
                    )
                    for cell in row
                ]
            )
    rows = int(getattr(table, "row_count", len(cells)) or 0)
    columns = int(
        getattr(
            table,
            "col_count",
            max((len(row) for row in cells), default=0),
        )
        or 0
    )
    return {"rows": rows, "columns": columns, "cells": cells}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _hwp_hwpx_parser(path: Path) -> dict[str, Any]:
    from hwp_hwpx_parser import Reader

    reader = Reader(path)
    try:
        extraction = reader.extract_text_with_notes()
        tables = [_table_summary(table) for table in reader.get_tables()]
        images = reader.get_images()
        return {
            "schema_version": 1,
            "parser": "hwp-hwpx-parser",
            "version": _package_version("hwp-hwpx-parser"),
            "text": str(extraction.text),
            "markdown": str(extraction.text),
            "tables": tables,
            "images_count": len(images),
            "pages_count": None,
            "footnotes_count": len(extraction.footnotes),
            "endnotes_count": len(extraction.endnotes),
            "links_count": len(extraction.hyperlinks),
            "metadata": {
                "file_type": reader.file_type.name,
                "is_valid": reader.is_valid,
                "is_encrypted": reader.is_encrypted,
                "memos_count": len(extraction.memos),
            },
            "warnings": [],
        }
    finally:
        reader.close()


def _openhanji_table(table: Any) -> dict[str, Any]:
    cells: list[list[str]] = []
    for row in getattr(table, "rows", []):
        cells.append(
            [
                str(getattr(cell, "text", ""))
                for cell in getattr(row, "cells", [])
            ]
        )
    return {
        "rows": len(cells),
        "columns": max((len(row) for row in cells), default=0),
        "cells": cells,
    }


def _openhanji(path: Path) -> dict[str, Any]:
    import openhanji

    document = openhanji.open(path, with_images=True)
    metadata = (
        _json_safe(document.metadata)
        if dataclasses.is_dataclass(document.metadata)
        else {}
    )
    return {
        "schema_version": 1,
        "parser": "openhanji",
        "version": _package_version("openhanji"),
        "text": document.to_text(),
        "markdown": document.to_markdown(),
        "tables": [_openhanji_table(table) for table in document.tables],
        "images_count": len(document.images),
        "pages_count": metadata.get("page_count"),
        "footnotes_count": 0,
        "endnotes_count": 0,
        "links_count": 0,
        "metadata": {**metadata, "sections_count": len(document.sections)},
        "warnings": [],
    }


def _python_hwpx(path: Path) -> dict[str, Any]:
    from hwpx import HwpxDocument

    document = HwpxDocument.open(path)
    try:
        table_map = document.get_table_map()
        raw_tables = table_map.get("tables", [])
        tables = [
            {
                "rows": int(table.get("rows", 0)),
                "columns": int(table.get("cols", 0)),
                "cells": [
                    {
                        "row": int(cell.get("row", 0)),
                        "column": int(cell.get("col", 0)),
                        "text": str(cell.get("text", "")),
                    }
                    for cell in table.get("cells", [])
                ],
            }
            for table in raw_tables
        ]
        return {
            "schema_version": 1,
            "parser": "python-hwpx",
            "version": _package_version("python-hwpx"),
            "text": document.export_text(),
            "markdown": document.export_markdown(),
            "tables": tables,
            "images_count": len(document.list_images()),
            "pages_count": None,
            "footnotes_count": 0,
            "endnotes_count": 0,
            "links_count": 0,
            "metadata": {"sections_count": len(document.sections)},
            "warnings": [],
        }
    finally:
        document.close()


def _pyhwp(path: Path, timeout_seconds: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="crewmeal-pyhwp-") as temporary:
        output_path = Path(temporary) / "output.txt"
        executable_name = "hwp5txt.exe" if sys.platform == "win32" else "hwp5txt"
        executable = Path(sys.executable).parent / executable_name
        if not executable.is_file():
            raise RuntimeError(f"hwp5txt entry point was not found: {executable}")
        completed = subprocess.run(
            [
                str(executable),
                "--output",
                str(output_path),
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"hwp5txt failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()[-2000:]}"
            )
        text = output_path.read_text(encoding="utf-8")
    return {
        "schema_version": 1,
        "parser": "pyhwp",
        "version": _package_version("pyhwp"),
        "text": text,
        "markdown": "",
        "tables": [],
        "images_count": 0,
        "pages_count": None,
        "footnotes_count": 0,
        "endnotes_count": 0,
        "links_count": 0,
        "metadata": {},
        "warnings": ["hwp5txt exposes text but not normalized structure counts."],
    }


def _run_rhwp(
    executable: Path,
    arguments: list[str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(executable), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"rhwp {' '.join(arguments[:1])} failed with exit "
            f"{completed.returncode}: {completed.stderr.strip()[-2000:]}"
        )
    return completed


def _rhwp(path: Path, executable: Path, timeout_seconds: float) -> dict[str, Any]:
    version_output = _run_rhwp(executable, ["--version"], timeout_seconds)
    version_match = re.search(
        r"\d+\.\d+\.\d+", f"{version_output.stdout}\n{version_output.stderr}"
    )
    with tempfile.TemporaryDirectory(prefix="crewmeal-rhwp-") as temporary:
        output_dir = Path(temporary)
        info = _run_rhwp(executable, ["info", str(path)], timeout_seconds)
        _run_rhwp(
            executable,
            ["export-markdown", str(path), "-o", str(output_dir)],
            timeout_seconds,
        )
        markdown_files = sorted(output_dir.glob("*.md"))
        if not markdown_files:
            raise RuntimeError("rhwp did not create a Markdown output file.")
        markdown = "\n\n".join(
            file.read_text(encoding="utf-8") for file in markdown_files
        )
        asset_files = [
            file
            for file in output_dir.rglob("*")
            if file.is_file() and file.suffix.lower() != ".md"
        ]
    page_match = re.search(r"페이지 수:\s*(\d+)", info.stdout)
    info_image_count = len(re.findall(r"^그림\d+\s", info.stdout, re.MULTILINE))
    markdown_image_count = len(re.findall(r"!\[[^\]]*]\([^)]*\)", markdown))
    tables = _markdown_tables(markdown)
    return {
        "schema_version": 1,
        "parser": "rhwp",
        "version": version_match.group(0) if version_match else "unknown",
        "text": _markdown_to_text(markdown),
        "markdown": markdown,
        "tables": tables,
        "images_count": max(
            len(asset_files), info_image_count, markdown_image_count
        ),
        "pages_count": int(page_match.group(1)) if page_match else None,
        "footnotes_count": 0,
        "endnotes_count": 0,
        "links_count": len(re.findall(r"\[[^\]]+]\(https?://", markdown)),
        "metadata": {},
        "warnings": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "engine",
        choices=[
            "hwp-hwpx-parser",
            "openhanji",
            "python-hwpx",
            "pyhwp",
            "rhwp",
        ],
    )
    parser.add_argument("document", type=Path)
    parser.add_argument("--rhwp-executable", type=Path)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    if args.engine == "hwp-hwpx-parser":
        result = _hwp_hwpx_parser(args.document)
    elif args.engine == "openhanji":
        result = _openhanji(args.document)
    elif args.engine == "python-hwpx":
        result = _python_hwpx(args.document)
    elif args.engine == "pyhwp":
        result = _pyhwp(args.document, args.timeout)
    else:
        if args.rhwp_executable is None:
            parser.error("--rhwp-executable is required for rhwp")
        result = _rhwp(args.document, args.rhwp_executable, args.timeout)

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
