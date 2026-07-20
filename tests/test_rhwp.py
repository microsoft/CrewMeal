import json
import subprocess
from pathlib import Path

import pytest

from crewmeal import rhwp

_PNG = b"\x89PNG\r\n\x1a\nfixture"


def test_extract_render_trees_validates_and_orders_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "tree"

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        assert command[1] == "export-render-tree"
        assert timeout_seconds == 10
        output_dir.mkdir(parents=True, exist_ok=True)
        for page_number in (2, 1):
            (output_dir / f"render_tree_{page_number:03d}.json").write_text(
                json.dumps({"type": "Page", "children": []}),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="내보내기 완료",
            stderr="LAYOUT_OVERFLOW page 2",
        )

    monkeypatch.setattr(rhwp, "_run_rhwp", fake_run)

    result = rhwp.extract_render_trees(
        tmp_path / "input.hwp",
        output_dir,
        rhwp_path=Path("rhwp"),
        timeout_seconds=10,
    )

    assert tuple(result.pages) == (1, 2)
    assert result.warnings == ("LAYOUT_OVERFLOW page 2",)


def test_extract_render_trees_detects_encryption_even_with_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        rhwp,
        "_run_rhwp",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["rhwp"],
            0,
            stdout="",
            stderr="오류: 암호화된 문서는 지원하지 않습니다",
        ),
    )

    with pytest.raises(rhwp.RhwpEncryptedError):
        rhwp.extract_render_trees(
            tmp_path / "input.hwp",
            tmp_path / "tree",
            rhwp_path=Path("rhwp"),
            timeout_seconds=10,
        )


def test_export_png_pages_requests_only_selected_zero_based_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requested: list[str] = []

    def fake_run(
        command: list[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout_seconds == 10
        requested.append(command[command.index("-p") + 1])
        page_dir = Path(command[command.index("-o") + 1])
        page_dir.joinpath("page.png").write_bytes(_PNG)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(rhwp, "_run_rhwp", fake_run)

    result = rhwp.export_png_pages(
        tmp_path / "input.hwpx",
        tmp_path / "png",
        (3, 1, 3),
        rhwp_path=Path("rhwp"),
        dpi=144,
        timeout_seconds=10,
    )

    assert requested == ["0", "2"]
    assert result.page_images == {1: _PNG, 3: _PNG}
