"""Read JATS full text (Europe PMC fullTextXML) as sections of plain text, and its tables cell by cell. Pure functions."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal

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


# ---- tables (<table-wrap>) -----------------------------------------------------------------------
# A table's cells are also part of its section's text (concatenated, so a numeric quote "matches" almost
# anywhere). Statistics from a table are checked here instead: by table label, row and column header.

_NUM = r"-?\d+(?:\.\d+)?"
_P = re.compile(rf"^(<=|>=|<|>|=)?\s*({_NUM})$")
_DF = re.compile(r"^([1-9]\d*)\s*[.,]\s*([1-9]\d*)$")      # d.f. never start with 0: "4.03" is an F, not (4, 3)
_MEAN_SPREAD = re.compile(rf"^({_NUM})(?:\s*\(\s*({_NUM})\s*\))?$")


@dataclass(frozen=True)
class Table:
    label: str                         # 'Table 3'
    caption: str
    header: tuple[str, ...]            # column names; an empty header cell is named 'col<n>' (1-based)
    rows: tuple[dict[str, str], ...]   # body rows, header → cell text; rowspans expanded, blank cells kept blank


def _cell_text(el: ET.Element) -> str:
    """Cell text with <break/> as a space ('β-Diversity<break/>Distance Matrix')."""
    parts = [el.text or ""]
    for child in el:
        parts.append(" " if child.tag == "break" else _cell_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _grid(trs: list[ET.Element]) -> list[list[str]]:
    """Rows of cell texts with rowspan/colspan expanded (a spanned cell's text repeats in every slot it covers)."""
    grid: list[list[str]] = []
    carry: dict[int, tuple[int, str]] = {}            # column → (rows still covered, text)
    for tr in trs:
        row: list[str] = []
        cells = [c for c in tr if c.tag in ("td", "th")]
        col = 0
        i = 0
        while i < len(cells) or col in carry:
            if col in carry:
                left, text = carry[col]
                row.append(text)
                carry[col] = (left - 1, text)
                if left - 1 == 0:
                    del carry[col]
                col += 1
                continue
            c = cells[i]
            i += 1
            text = norm(_cell_text(c))
            rs, cs = int(c.get("rowspan") or 1), int(c.get("colspan") or 1)
            for _ in range(cs):
                row.append(text)
                if rs > 1:
                    carry[col] = (rs - 1, text)
                col += 1
        grid.append(row)
    return grid


def tables(jats: bytes) -> dict[str, Table]:
    """Every <table-wrap> with a label, by label. Header = the last <thead> row (earlier ones joined with ' / ')."""
    root = ET.fromstring(jats)
    out: dict[str, Table] = {}
    for tw in root.iter("table-wrap"):
        label_el, cap_el = tw.find("label"), tw.find("caption")
        if label_el is None:
            continue
        label = norm(_cell_text(label_el))
        thead, tbody = tw.find(".//thead"), tw.find(".//tbody")
        head_rows = _grid(thead.findall("tr")) if thead is not None else []
        width = max((len(r) for r in head_rows), default=0)
        header = []
        for j in range(width):
            parts = [r[j] for r in head_rows if j < len(r) and r[j]]
            name = " / ".join(dict.fromkeys(parts))
            header.append(name or f"col{j + 1}")
        body = _grid(tbody.findall("tr")) if tbody is not None else []
        rows = tuple({h: (r[j] if j < len(r) else "") for j, h in enumerate(header)} for r in body)
        out[label] = Table(label, norm(_cell_text(cap_el)) if cap_el is not None else "", tuple(header), rows)
    return out


def find_rows(table: Table, match: dict[str, str]) -> list[int]:
    """Indexes of the body rows whose cells equal `match` (header → text). For the match columns only, a blank
    cell takes the value above it (Table 3's 'Caecum' heads two rows, the second blank): lookup, never storage."""
    want = {norm(k): norm(v) for k, v in match.items()}
    hits, last = [], {k: "" for k in want}
    for i, row in enumerate(table.rows):
        for k in want:
            if row.get(k):
                last[k] = row[k]
        if all(k in table.header and last[k] == v for k, v in want.items()):
            hits.append(i)
    return hits


def parse_p(cell: str) -> tuple[str, Decimal]:
    """'<0.001' → ('<', 0.001); '0.023' → ('=', 0.023)."""
    m = _P.match(norm(cell))
    if not m:
        raise ValueError(f"not a p value: {cell!r}")
    return m.group(1) or "=", Decimal(m.group(2))


def parse_number(cell: str) -> Decimal:
    """A plain number. '<0.01' is refused: there is no column for a statistic's operator."""
    m = _P.match(norm(cell))
    if not m:
        raise ValueError(f"not a number: {cell!r}")
    if m.group(1):
        raise ValueError(f"printed with an operator ({cell!r}); no column holds a test statistic's operator")
    return Decimal(m.group(2))


def parse_df(cell: str) -> tuple[int, int]:
    """Degrees of freedom printed with the comma lost or kept: '2.61' / '2, 61' → (2, 61).
    Both parts are positive integers without a leading zero, so an F like '4.03' or '0.25' is refused."""
    m = _DF.match(norm(cell))
    if not m:
        raise ValueError(f"not a d.f. pair: {cell!r}")
    return int(m.group(1)), int(m.group(2))


def parse_mean_spread(cell: str) -> tuple[Decimal, Decimal | None]:
    """'325.25 (11.01)' → (325.25, 11.01); '7.34' → (7.34, None)."""
    m = _MEAN_SPREAD.match(norm(cell))
    if not m:
        raise ValueError(f"not 'mean (spread)': {cell!r}")
    return Decimal(m.group(1)), Decimal(m.group(2)) if m.group(2) else None
