#!/usr/bin/env python3
"""Shared keyword matching and ATS fetchers for the CC-jobs crawlers.

Both `crawler.py` (CCC members) and `non_member_jobs_aggregator.py` (the wider
market) need the same two things: a way to decide whether a blob of job text is
about confidential computing, and a way to pull jobs out of a hosted ATS. They
used to each carry their own copy and the copies drifted — the aggregator's
matcher did bare substring matching, so "enclave" hit "enclaved" and its
acronym handling was unusable. This module is the single source of truth.

Every fetcher returns `(jobs, error)` where each job is a dict with the keys
`title`, `url`, `location`, `dept` and `text`. `text` concatenates the title,
team and full description, so a keyword buried in the body is still found.
"""

import html
import re

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TIMEOUT = 30


# ── Text extraction ──────────────────────────────────────────────────────────

def html_to_text(raw: str) -> str:
    """Strip markup to plain text.

    Greenhouse returns its `content` field HTML-escaped ("&lt;p&gt;"), so a
    single BeautifulSoup pass leaves literal "<p>" tags in the output. Unescape
    first, then strip, so tag names never leak into the matched text.
    """
    if not raw:
        return ""
    return BeautifulSoup(html.unescape(raw), "lxml").get_text(" ", strip=True)


# ── False-positive suppression ───────────────────────────────────────────────

# Phrases where a CC acronym means something else entirely. Each pattern must
# itself contain the acronym, because the check requires the candidate match to
# fall *inside* the offending phrase. A looser "is there a bad phrase nearby"
# test wrongly discards real hits: a posting that says "our TEE team runs
# incident reviews" and later "we ship Intel TDX" must still match on TDX.
_FALSE_POSITIVE_PHRASES = [
    # Datadog names an internal team "Technical Escalation Engineering (TEE)"
    r"technical escalation\w*\s+engineering\s*\(\s*TEE\s*\)",
    r"\bTEE\s+team\b",
    # JPMorgan's HR org is "Talent & Employee Experience (TEE)"
    r"employee\s+experience\s*\(\s*TEE\s*\)",
    r"\bwithin\s+TEE\b",
    # Robinhood and others use "SEV" for incident severity, not AMD SEV
    r"\bSEV\s*[-\u2013]?\s*\d+\b",
    r"\bSEV\s+(?:review|process|triage|response|postmortem)\w*",
    r"postmortems,\s*SEV",
    # Government/defence postings use "enclave" for an isolated network segment
    # (Microsoft CTJ roles: "mission enclaves", "enterprise and enclave level")
    r"\b(?:mission|network|classified|tactical)\s+enclaves?\b",
    r"\benclaves?\s+(?:level|boundar\w*|network\w*)\b",
    r"\benterprise\s+and\s+enclave\b",
    r"\bsplit-access\s+and\s+enclave\b",
    r"\benclave\s+isolation\s+and\s+boundary\b",
    # MITRE: "operating in an isolated enclave", "enclave or air-gapped
    # environments". The lookbehind keeps "hardware-isolated enclave" (CC).
    r"(?<![\w-])isolated\s+enclaves?\b",
    r"\benclaves?\s+or\s+air-gapped\b",
    r"\bair-gapped\s+(?:or\s+)?enclaves?\b",
]

_FP_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _FALSE_POSITIVE_PHRASES]


def _is_false_positive(text: str, start: int, end: int) -> bool:
    """True if this specific match sits inside a known non-CC use."""
    for p in _FP_PATTERNS:
        for mo in p.finditer(text):
            if mo.start() <= start and end <= mo.end():
                return True
    return False


# Keywords that never take a plural "s". "SEVs" in a posting is nearly always
# incident severities ("respond to SEVs"), not AMD SEV.
NO_PLURAL = {"SEV"}


# ── Keyword matching ─────────────────────────────────────────────────────────

def build_matchers(config: dict):
    """Compile keyword config into (case_sensitive, case_insensitive) patterns.

    Both sets are word-boundary anchored. That matters for the short acronyms:
    an unanchored "TEE" matches "COMMITTEE" and an unanchored "enclave"
    matches "enclaved" in unrelated prose.
    The boundary is ASCII-only, not `\\b`: Chinese postings (ByteDance) run
    words together, and Python counts CJK characters as word characters, so
    `\\bTEE\\b` misses "支持TEE" and `\\b机密计算\\b` misses "的机密计算技术".
    Latin keywords also accept a plural "s" ("TEEs", "enclaves",
    "confidential VMs"): the trailing boundary alone rejected every plural.
    The "s" is a named group so matched_keywords can report the singular.
    Other suffixes ("enclaved", "TEEing") still fail the boundary.
    """
    def _anchored(kw):
        pat = re.escape(kw)
        if re.match(r"[A-Za-z0-9_]", kw):
            pat = r"(?<![A-Za-z0-9_])" + pat
        if re.search(r"[A-Za-z0-9_]$", kw):
            if kw.upper() not in NO_PLURAL:
                pat += r"(?P<pl>s)?"
            pat += r"(?![A-Za-z0-9_])"
        return pat

    cs_patterns = [
        re.compile(_anchored(kw))
        for kw in config.get("case_sensitive", [])
    ]
    ci_patterns = [
        re.compile(_anchored(kw.lower()), re.IGNORECASE)
        for kw in config.get("case_insensitive", [])
    ]
    return cs_patterns, ci_patterns


def _real_hits(text: str, patterns) -> list[str]:
    """Keyword occurrences that survive false-positive filtering.

    A keyword counts if *any* of its occurrences is legitimate, so one bogus
    use doesn't veto a genuine one elsewhere in the same posting.
    """
    found = []
    for pat in patterns:
        for mo in pat.finditer(text):
            if not _is_false_positive(text, mo.start(), mo.end()):
                hit = mo.group(0)
                if "pl" in pat.groupindex and mo.group("pl"):
                    hit = hit[:-1]  # report "TEEs" as TEE
                found.append(hit)
                break
    return found


def matches(text: str, cs_patterns, ci_patterns) -> bool:
    if not text:
        return False
    return bool(_real_hits(text, cs_patterns) or _real_hits(text, ci_patterns))


def matched_keywords(text: str, cs_patterns, ci_patterns) -> list[str]:
    """Which keywords fired — used to show *why* a job was flagged.

    A bare boolean makes false positives hard to audit, so the report prints
    these next to each role for a human to sanity-check.
    """
    if not text:
        return []
    hits = _real_hits(text, cs_patterns) + _real_hits(text, ci_patterns)
    return sorted({h if h.isupper() else h.lower() for h in hits})


# Keywords that are also ordinary English. "Trusted execution" turns up in
# non-technical postings ("a trusted execution partner across our governance
# portfolio" in a Cohere legal role). A posting whose *only* hits are weak is
# reported for human review instead of as a confirmed CC role; one strong hit
# alongside makes it confirmed.
WEAK_KEYWORDS = {"trusted execution"}


def only_weak_hits(keywords: list[str]) -> bool:
    """True if every matched keyword is weak (and there is at least one)."""
    return bool(keywords) and all(k.lower() in WEAK_KEYWORDS for k in keywords)


# ── ATS fetchers ─────────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str) -> tuple[list[dict], str | None]:
    """Greenhouse job board API. `content=true` inlines full descriptions."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    result = []
    for j in r.json().get("jobs", []):
        content = html_to_text(j.get("content", "") or "")
        result.append({
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": ", ".join(o.get("name", "") for o in j.get("offices", [])),
            "dept": ", ".join(d.get("name", "") for d in j.get("departments", [])),
            "text": f"{j.get('title', '')} {content}",
        })
    return result, None


def fetch_lever(slug: str) -> tuple[list[dict], str | None]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    result = []
    for p in r.json():
        cats = p.get("categories", {})
        desc = p.get("descriptionPlain", "") or ""
        title = p.get("text", "")
        result.append({
            "title": title,
            "url": p.get("hostedUrl", ""),
            "location": cats.get("location", ""),
            "dept": cats.get("team", ""),
            "text": f"{title} {cats.get('team', '')} {desc}",
        })
    return result, None


def fetch_ashby(slug: str) -> tuple[list[dict], str | None]:
    """Ashby's public posting API.

    This is the `posting-api/job-board` endpoint, not the GraphQL board the
    careers site itself calls. The GraphQL list query returns titles only and
    needs a second round trip per job; this one inlines `descriptionPlain` for
    every posting in a single request.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    result = []
    for j in r.json().get("jobs", []):
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        title = j.get("title", "")
        team = j.get("team") or j.get("department") or ""
        result.append({
            "title": title,
            "url": j.get("jobUrl", ""),
            "location": j.get("location") or "",
            "dept": team,
            "text": f"{title} {team} {desc}",
        })
    return result, None


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_board(ats: str, slug: str) -> tuple[list[dict], str | None]:
    """Dispatch to the right fetcher, or report an unknown ATS explicitly.

    Returning an error rather than an empty list keeps a typo'd `ats` value
    visible in the report instead of silently contributing zero jobs.
    """
    fn = FETCHERS.get(ats)
    if fn is None:
        return [], f"unknown ATS '{ats}'"
    return fn(slug)


def dedupe_jobs(jobs: list[dict], by_content: bool = False) -> list[dict]:
    """Drop repeated postings.

    Default keys on URL, which catches a board returning the same posting
    twice. `by_content=True` keys on (company, title, location) instead, which
    additionally collapses distinct requisition IDs for what is plainly one
    role — Databricks lists "Staff Product Manager, Security" in San Francisco
    under two `gh_jid`s, and counting that as two open roles overstates the
    market.
    """
    seen = set()
    out = []
    for j in jobs:
        if by_content:
            key = (j.get("company", ""), j.get("title", "").strip().lower(),
                   j.get("location", ""))
        else:
            key = j.get("url") or (j.get("company", ""), j.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out
