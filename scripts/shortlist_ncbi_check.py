"""Step 1, pass 2: check shortlisted PGMD BioProjects against NCBI and Europe PMC.

For each BioProject:
  - BioSamples (all attributes) via E-utilities: is there a per-animal id (host_subject_id or
    similar), and is each animal sampled at several gut sites? Any pen / litter attributes?
  - SRA run info: runs, layout, platform/instrument, depth.
  - The paper (from PGMD's Link, else a title search) in Europe PMC: PMCID, open access, licence.

Exploration, not the Step 2 client. Raw responses are kept under <out_dir>/<bioproject>/ so the
analysis can be redone without refetching; a one-row-per-project summary goes to stdout.

    python scripts/shortlist_ncbi_check.py data/pgmd_projects.tsv data/pass2 PRJNA380495 …

NCBI_API_KEY (optional, env) raises the E-utilities limit from 3 to 10 requests/s.
NCBI_EMAIL (optional, env) is sent as `email`, as NCBI asks.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TOOL = "ncbi-api-shortlist"

SUBJECT_NAMES = ("host_subject_id", "host_subject id", "subject_id", "animal_id", "animal id",
                 "host_animal_id", "pig_id", "pig id", "individual", "host_id", "animal")
SITE_NAMES = ("tissue", "isolation_source", "body_site", "host_body_site", "host_tissue_sampled",
              "sample_type", "env_local_scale", "env_medium", "body site", "sampling_site", "site")
HOUSING_HINTS = ("pen", "litter", "sow", "dam", "block", "replicate", "room", "batch")
TREATMENT_HINTS = ("treatment", "diet", "group", "condition")


class Fetcher:
    """Polite HTTP: fixed spacing per host, retries on 429/5xx with backoff."""

    def __init__(self) -> None:
        self.api_key = os.environ.get("NCBI_API_KEY", "")
        self.email = os.environ.get("NCBI_EMAIL", "")
        self.ncbi_gap = 0.11 if self.api_key else 0.34
        self._last: dict[str, float] = defaultdict(float)

    def get(self, url: str, params: dict[str, str], data: dict[str, str] | None = None) -> bytes:
        host = urllib.parse.urlparse(url).netloc
        if "ncbi" in host:
            params = {**params, "tool": TOOL}
            if self.api_key:
                params["api_key"] = self.api_key
            if self.email:
                params["email"] = self.email
        full = f"{url}?{urllib.parse.urlencode(params)}"
        body = urllib.parse.urlencode(data).encode() if data else None
        for attempt in range(6):
            wait = (self.ncbi_gap if "ncbi" in host else 0.2) - (time.monotonic() - self._last[host])
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(full, data=body), timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    retry = e.headers.get("Retry-After")
                    time.sleep(float(retry) if retry and retry.isdigit() else 2 ** attempt)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"unreachable: {full}")


def eutils_json(f: Fetcher, tool: str, **params: str) -> dict:
    return json.loads(f.get(f"{EUTILS}/{tool}.fcgi", {**params, "retmode": "json"}))


def bioproject_uid(f: Fetcher, acc: str) -> str | None:
    ids = eutils_json(f, "esearch", db="bioproject", term=f"{acc}[PRJA]")["esearchresult"]["idlist"]
    return ids[0] if ids else None


def biosample_uids(f: Fetcher, bp_uid: str) -> list[str]:
    res = eutils_json(f, "elink", dbfrom="bioproject", db="biosample", id=bp_uid)
    for ls in res.get("linksets", []):
        for db in ls.get("linksetdbs", []):
            if db.get("linkname") == "bioproject_biosample":
                return [str(x) for x in db.get("links", [])]
    return []


def fetch_biosamples(f: Fetcher, uids: list[str]) -> bytes:
    """One XML document holding every BioSample (POST, 200 ids per call)."""
    parts: list[bytes] = []
    for i in range(0, len(uids), 200):
        xml = f.get(f"{EUTILS}/efetch.fcgi", {"db": "biosample", "retmode": "xml"},
                    data={"id": ",".join(uids[i:i + 200])})
        root = ET.fromstring(xml)
        parts.extend(ET.tostring(bs) for bs in root.findall("BioSample"))
    return b"<BioSampleSet>" + b"".join(parts) + b"</BioSampleSet>"


def fetch_runinfo(f: Fetcher, acc: str) -> bytes:
    res = eutils_json(f, "esearch", db="sra", term=f"{acc}[BioProject]", usehistory="y")["esearchresult"]
    if int(res.get("count", 0)) == 0:
        return b""
    return f.get(f"{EUTILS}/efetch.fcgi", {"db": "sra", "rettype": "runinfo", "retmode": "text",
                                           "WebEnv": res["webenv"], "query_key": res["querykey"]})


def paper_query(link: str, title: str) -> str | None:
    if m := re.search(r"PMC\d+", link):
        return f"PMCID:{m.group(0)}"
    if m := re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", link):
        return f"EXT_ID:{m.group(1)} AND SRC:MED"
    if m := re.search(r"(10\.\d{4,9}/[^\s?#]+)", link):
        return f'DOI:"{m.group(1).rstrip("/.")}"'
    if m := re.search(r"nature\.com/articles/([^\s?#/]+)", link):
        return f'DOI:"10.1038/{m.group(1)}"'
    t = re.sub(r"[^\w\s-]", " ", title.rstrip("…")).strip()
    return f'TITLE:"{t}"' if t else None


def fetch_paper(f: Fetcher, query: str) -> bytes:
    return f.get(EPMC, {"query": query, "resultType": "core", "format": "json", "pageSize": "3"})


# ── analysis of what was fetched ────────────────────────────────────────────

def parse_biosamples(xml: bytes) -> list[dict[str, str]]:
    out = []
    for bs in ET.fromstring(xml).findall("BioSample"):
        rec = {"_accession": bs.get("accession", "")}
        for a in bs.findall("./Attributes/Attribute"):
            name = (a.get("harmonized_name") or a.get("attribute_name") or "").strip().lower()
            rec[name] = (a.text or "").strip()
        out.append(rec)
    return out


def pick(names: set[str], wanted: tuple[str, ...]) -> list[str]:
    return [n for n in wanted if n in names]


def summarise(acc: str, samples: list[dict[str, str]], runinfo: bytes, paper: bytes) -> dict[str, str]:
    names = {k for s in samples for k in s if not k.startswith("_")}
    distinct = {n: len({s.get(n, "") for s in samples}) for n in names}
    subj_attr = next(iter(pick(names, SUBJECT_NAMES)), "")
    site_attrs = sorted(pick(names, SITE_NAMES), key=lambda n: -distinct[n])
    site_attr = next((n for n in site_attrs if distinct[n] > 1), site_attrs[0] if site_attrs else "")

    per_subject: dict[str, set[str]] = defaultdict(set)
    n_per_subject: Counter[str] = Counter()
    if subj_attr:
        for s in samples:
            sid = s.get(subj_attr, "")
            n_per_subject[sid] += 1
            per_subject[sid].add(s.get(site_attr, ""))
    multi_site = sum(1 for v in per_subject.values() if len(v) >= 2)

    runs = list(csv.DictReader(runinfo.decode().splitlines())) if runinfo.strip() else []
    runs = [r for r in runs if r.get("Run", "").startswith(("SRR", "ERR", "DRR"))]
    spots = [int(r["spots"]) for r in runs if (r.get("spots") or "").isdigit()]

    hits = json.loads(paper).get("resultList", {}).get("result", []) if paper else []
    h = hits[0] if hits else {}
    return {
        "bioproject": acc,
        "ncbi_biosamples": str(len(samples)),
        "subject_attr": subj_attr,
        "n_subjects": str(len(n_per_subject)) if subj_attr else "",
        "max_samples_per_subject": str(max(n_per_subject.values(), default=0)) if subj_attr else "",
        "site_attr": site_attr,
        "sites": "|".join(f"{k}:{n}" for k, n in Counter(s.get(site_attr, "") for s in samples).most_common(8)) if site_attr else "",
        "subjects_multi_site": str(multi_site) if subj_attr else "",
        "housing_attrs": ",".join(n for n in sorted(names) if any(x in n for x in HOUSING_HINTS)),
        "treatment_attrs": ",".join(f"{n}:{distinct[n]}" for n in sorted(names) if any(x in n for x in TREATMENT_HINTS)),
        "attributes": ",".join(f"{n}:{distinct[n]}" for n in sorted(names)),
        "sra_runs": str(len(runs)),
        "layouts": "|".join(f"{k}:{n}" for k, n in Counter(r.get("LibraryLayout", "") for r in runs).items()),
        "instruments": "|".join(f"{k}:{n}" for k, n in Counter(r.get("Model", "") for r in runs).items()),
        "median_spots": str(int(statistics.median(spots))) if spots else "",
        "epmc_hits": str(len(hits)),
        "pmid": h.get("pmid", ""), "pmcid": h.get("pmcid", ""), "doi": h.get("doi", ""),
        "is_open_access": h.get("isOpenAccess", ""), "in_epmc": h.get("inEPMC", ""),
        "license": h.get("license", ""),
        "journal": (h.get("journalInfo") or {}).get("journal", {}).get("title", ""),
        "year": h.get("pubYear", ""),
        "title": h.get("title", "")[:150],
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pgmd_tsv", type=Path, help="output of pgmd_shortlist.py")
    ap.add_argument("out_dir", type=Path, help="raw responses go to <out_dir>/<bioproject>/")
    ap.add_argument("bioprojects", nargs="+")
    args = ap.parse_args(argv)

    pgmd = {r["bioproject"]: r for r in csv.DictReader(args.pgmd_tsv.open(), delimiter="\t")}
    f = Fetcher()
    rows = []
    for acc in args.bioprojects:
        d = args.out_dir / acc
        d.mkdir(parents=True, exist_ok=True)
        print(f"{acc}: fetching", file=sys.stderr)
        uid = bioproject_uid(f, acc)
        xml = fetch_biosamples(f, biosample_uids(f, uid)) if uid else b"<BioSampleSet/>"
        (d / "biosample.xml").write_bytes(xml)
        runinfo = fetch_runinfo(f, acc)
        (d / "runinfo.csv").write_bytes(runinfo)
        p = pgmd.get(acc, {})
        q = paper_query(p.get("p_link", ""), p.get("p_title", ""))
        paper = fetch_paper(f, q) if q else b""
        (d / "europepmc.json").write_bytes(paper)
        (d / "query.txt").write_text(f"bioproject_uid={uid}\nepmc_query={q}\n")
        rows.append(summarise(acc, parse_biosamples(xml), runinfo, paper))

    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)


if __name__ == "__main__":
    main()
