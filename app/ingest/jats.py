"""Read JATS full text (Europe PMC fullTextXML) as sections of plain text. Pure functions."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_SPACE = re.compile(r"\s+")
# typographic variants that papers and their quotes mix freely
_FOLD = str.maketrans({"′": "'", "’": "'", "‘": "'", "“": '"', "”": '"',
                       "–": "-", "—": "-", "−": "-", " ": " "})


def norm(text: str) -> str:
    """Whitespace collapsed and typographic quotes/dashes folded, so a quote matches the source text."""
    return _SPACE.sub(" ", text.translate(_FOLD)).strip()


@dataclass(frozen=True)
class Section:
    path: tuple[str, ...]          # ('Materials and Methods', 'Statistical Analysis')
    text: str                      # this section's own text (paragraphs, formulas), not its subsections

    @property
    def title(self) -> str:
        return self.path[-1] if self.path else ""


def _own_text(sec: ET.Element) -> str:
    parts = []
    for child in sec:
        if child.tag in ("sec", "title"):
            continue
        parts.append("".join(child.itertext()))
    return norm(" ".join(parts))


def sections(jats: bytes) -> list[Section]:
    """Every <sec> in body and back matter, depth-first, with its title path."""
    root = ET.fromstring(jats)
    out: list[Section] = []

    def walk(sec: ET.Element, path: tuple[str, ...]) -> None:
        title_el = sec.find("title")
        title = norm("".join(title_el.itertext())) if title_el is not None else ""
        p = path + (title,)
        out.append(Section(p, _own_text(sec)))
        for child in sec.findall("sec"):
            walk(child, p)

    for container in (root.find(".//body"), root.find(".//back")):
        if container is not None:
            for sec in container.findall("sec"):
                walk(sec, ())
    return out


def find_quote(jats: bytes, quote: str, section_title: str | None = None) -> list[Section]:
    """Sections (optionally only those titled `section_title`) whose text contains the quote verbatim."""
    q = norm(quote)
    return [s for s in sections(jats)
            if (section_title is None or s.title == norm(section_title)) and q in s.text]
