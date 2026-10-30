from __future__ import annotations

from pathlib import Path

from scripts import check_doc_links


def test_link_checker_skips_build_and_generated_directories(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "README.md").write_text("[existing](docs/page.md)\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text("# Docs\n", encoding="utf-8")
    generated_markdown = [
        tmp_path / "build" / "lib" / "README.md",
        tmp_path / "dist" / "docs" / "README.md",
        tmp_path / "package.egg-info" / "README.md",
        tmp_path / "node_modules" / "package" / "README.md",
    ]
    for path in generated_markdown:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[not present](excluded-target.md)\n", encoding="utf-8")

    monkeypatch.setattr(check_doc_links, "ROOT", tmp_path)

    assert check_doc_links.main() == 0
    assert "Markdown relative links: ok" in capsys.readouterr().out
