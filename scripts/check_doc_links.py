#!/usr/bin/env python3
"""Check repository-relative Markdown links and image paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
IGNORED_DIRECTORIES = {".git", ".venv", "node_modules", "resources", "build", "dist"}
LINK = re.compile(r"!?\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")


def main() -> int:
    errors: list[str] = []
    for markdown in sorted(ROOT.rglob("*.md")):
        relative = markdown.relative_to(ROOT)
        if any(part in IGNORED_DIRECTORIES or part.endswith(".egg-info") for part in relative.parts[:-1]):
            continue
        text = markdown.read_text(encoding="utf-8")
        text = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.S)
        for match in LINK.finditer(text):
            target = unquote(match.group(1) or match.group(2)).split("#", 1)[0]
            if not target:
                continue
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith(("//", "mailto:")):
                continue
            path = (markdown.parent / parsed.path).resolve()
            if not path.is_relative_to(ROOT) or not path.exists():
                errors.append(f"{markdown.relative_to(ROOT)}: missing link target {target}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Markdown relative links: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
