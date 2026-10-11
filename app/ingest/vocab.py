"""Rule dictionaries that map source strings to ontology terms (design doc §8, "rules first").

Used at read time by reports. Loaders keep raw values, so a dictionary change never needs a reload.
Only terms already used in the design doc are listed; anything else stays unmapped, and visible.
"""
from __future__ import annotations

import re

# UBERON gut sites (design doc / handoff Step 3)
GUT_SITE_TERMS: dict[str, tuple[str, str]] = {
    "feces": ("UBERON:0001988", "feces"),
    "rectum": ("UBERON:0001052", "rectum"),
    "colon": ("UBERON:0001155", "colon"),
    "ileum": ("UBERON:0002116", "ileum"),
    "cecum": ("UBERON:0001153", "caecum"),
    "jejunum": ("UBERON:0002115", "jejunum"),
    "duodenum": ("UBERON:0002114", "duodenum"),
    "stomach": ("UBERON:0000945", "stomach"),
}
_SITE_WORDS: dict[str, str] = {
    "feces": "feces", "faeces": "feces", "fecal": "feces", "faecal": "feces", "stool": "feces",
    "rectum": "rectum", "rectal": "rectum",
    "colon": "colon", "colonic": "colon",
    "ileum": "ileum", "ileal": "ileum",
    "cecum": "cecum", "caecum": "cecum", "cecal": "cecum", "caecal": "cecum",
    "jejunum": "jejunum", "jejunal": "jejunum", "jej": "jejunum",
    "duodenum": "duodenum", "duodenal": "duodenum",
    "stomach": "stomach", "gastric": "stomach",
}
# what was sampled at the site: separate from the site, never folded into it
_MATERIAL_WORDS: dict[str, str] = {
    "mucosa": "mucosa", "mucosal": "mucosa", "scraping": "mucosa",
    "digesta": "digesta", "content": "digesta", "contents": "digesta", "lumen": "digesta",
    "luminal": "digesta",
    "swab": "swab",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def gut_sites(text: str | None) -> set[str]:
    """Every UBERON gut-site term named in the text ('cecum mucosa' → {UBERON:0001153})."""
    if not text:
        return set()
    return {GUT_SITE_TERMS[_SITE_WORDS[w]][0] for w in _words(text) if w in _SITE_WORDS}


def material(text: str | None) -> str | None:
    if not text:
        return None
    found = {_MATERIAL_WORDS[w] for w in _words(text) if w in _MATERIAL_WORDS}
    return found.pop() if len(found) == 1 else None


def site_label(term: str) -> str:
    return next((label for t, label in GUT_SITE_TERMS.values() if t == term), term)
