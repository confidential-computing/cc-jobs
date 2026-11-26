#!/usr/bin/env python3
"""CCC member job board crawler — finds Confidential Computing roles."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import feedparser
import yaml
from bs4 import BeautifulSoup

from cc_common import (
    HEADERS,
    build_matchers,
    fetch_greenhouse,
    fetch_lever,
    html_to_text,
    matches,
)


CCC_PROJECT_UID = "54ec092e-cadd-49af-9edf-9a5c888cc283"
MEMBERS_TTL_SECONDS = 7 * 24 * 60 * 60  # one week



_LFX_TIER_MAP = {
    "Premier Membership": "Premier",
    "General Membership": "General",
    "Startup Membership": "Startup",
    "Associate Membership": "Associate",
}
# Higher rank wins when an org appears in multiple tiers
_TIER_RANK = {"Associate": 1, "Startup": 2, "General": 3, "Premier": 4}

_STRIP_LEGAL = re.compile(
    r"\s+(Inc\.?|LLC|Ltd\.?|Limited|Corporation|Corp\.?|Co\.?|LP|LLP|GmbH|S\.A\.)$",
    flags=re.IGNORECASE,
)


def _normalize(name: str) -> str:
    # Handle non-string input gracefully  
    if not isinstance(name, str):
        name = str(name)
    name = _STRIP_LEGAL.sub("", name).strip(" ,.")
    return re.sub(r"\s+", " ", name).lower()


def fetch_lfx_members(cache_path: Path) -> list[dict]:
    # Direct API call to LFX instead of using Claude CLI + MCP
    api_url = "https://api-gw.platform.linuxfoundation.org/project-service/v1/public/projects/a092M00001IfDuzQAF/members"
    params = {"status": "Active", "orderBy": "name"}
    
    try:
        response = requests.get(api_url, params=params, timeout=30)
        response.raise_for_status()
        raw_members = response.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to fetch LFX members: {e}") from e
    
    if not isinstance(raw_members, list):
        raise ValueError(f"Expected list from LFX API, got: {type(raw_members)}")

    # Convert API response to expected format
    members = []
    for member in raw_members:
        if not isinstance(member, dict):
            continue
        name = member.get("Name", "").strip()
        membership = member.get("Membership", {})
        tier_raw = membership.get("Name", "") if isinstance(membership, dict) else ""
        
        if name and tier_raw:
            members.append({"name": name, "tier": tier_raw})

    # Deduplicate: an org can appear in multiple tiers; keep the highest-ranked one.
    best: dict[str, dict] = {}
    for m in members:
        name = m.get("name", "").strip()
        tier = _LFX_TIER_MAP.get(m.get("tier", ""), "General")
        norm = _normalize(name)
        if norm and (_TIER_RANK.get(tier, 0) > _TIER_RANK.get(best.get(norm, {}).get("tier", ""), 0)):
            best[norm] = {"name": name, "tier": tier}

    out = sorted(best.values(), key=lambda m: m["name"])

    tmp = str(cache_path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"members": out}, f, indent=2)
    os.replace(tmp, cache_path)

    return out


def load_lfx_members(cache_path: Path) -> list[dict] | None:
    try:
        data = json.loads(cache_path.read_text()).get("members")
        if not data:
            return None
        # Upgrade old format (list of strings) to list of dicts
        if isinstance(data[0], str):
            return [{"name": n, "tier": "General"} for n in data]
        return data
    except FileNotFoundError:
        return None


def _remove_member_block(text: str, name: str) -> str:
    """Remove a single member's YAML block from raw file text."""
    lines = text.splitlines(keepends=True)
    target = f'  - name: {name}'
    start = next((i for i, l in enumerate(lines) if l.rstrip('\n') == target), None)
    if start is None:
        return text
    # Walk back over any preceding blank line so we don't leave a double blank
    block_start = start - 1 if start > 0 and lines[start - 1].strip() == '' else start
    # The block ends at the next member entry, section comment, or EOF
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i].rstrip('\n')
        if s.startswith('  - name:') or s.startswith('  # ──'):
            end = i
            break
    del lines[block_start:end]
    return ''.join(lines)


_ENTRY_FIELD_ORDER = [
    "type", "slug", "workday_api", "workday_job_base",
    "rss_url", "api_url", "careers_url", "note",
]


def _member_entry_text(name: str, tier: str, carry: dict | None = None) -> str:
    """Render a members.yaml entry.

    When `carry` is supplied (a renamed org's previous config), its crawl
    settings are preserved so a working integration survives the rename.
    """
    out = f'\n  - name: {name}\n    tier: {tier}\n'
    if not carry:
        return (
            out
            + '    type: no_page\n'
            + '    note: "Added from LFX — update with correct careers page info"\n'
        )
    for key in _ENTRY_FIELD_ORDER:
        if key not in carry:
            continue
        val = carry[key]
        if key == "note":
            val = f'"{str(val).rstrip()}"'
        out += f'    {key}: {val}\n'
    return out


def _append_member_block(text: str, name: str, tier: str, carry: dict | None = None) -> str:
    """Append a new member entry into the correct tier section of the YAML text."""
    lines = text.splitlines(keepends=True)
    section_marker = f'  # ── {tier}'

    # Find the start of this tier's section
    section_line = next(
        (i for i, l in enumerate(lines) if l.rstrip('\n').startswith(section_marker)), None
    )

    if section_line is None:
        # Section doesn't exist yet — insert it before the Associate section (or at EOF)
        assoc_line = next(
            (i for i, l in enumerate(lines) if l.rstrip('\n').startswith('  # ── Associate')), len(lines)
        )
        dashes = '─' * max(0, 68 - len(tier))
        header = f'\n  # ── {tier} {dashes}\n'
        lines.insert(assoc_line, header + _member_entry_text(name, tier, carry))
        return ''.join(lines)

    # Find the end of this section (next section comment or EOF)
    section_end = next(
        (i for i in range(section_line + 1, len(lines)) if lines[i].rstrip('\n').startswith('  # ──')),
        len(lines),
    )

    lines.insert(section_end, _member_entry_text(name, tier, carry))
    return ''.join(lines)


def sync_members_yaml(
    lfx_members: list[dict], yaml_path: Path, yaml_members: list[dict]
) -> tuple[list[str], list[str]]:
    """Add new LFX members and remove departed ones from members.yaml.

    Returns (added_names, removed_names).
    """
    lfx_by_norm = {_normalize(m["name"]): m for m in lfx_members}
    yaml_by_norm = {_normalize(m["name"]): m["name"] for m in yaml_members}

    to_add = [m for norm, m in lfx_by_norm.items() if norm not in yaml_by_norm]
    to_remove = [name for norm, name in yaml_by_norm.items() if norm not in lfx_by_norm]

    if not to_add and not to_remove:
        return [], []

    # Guard against LFX renames silently discarding a working integration.
    # If a departing org shares a leading token-prefix with an arriving one,
    # it is almost certainly a rename (e.g. "Hewlett Packard Enterprise
    # Development LP" -> "Hewlett Packard Enterprise"). Carry the old config
    # forward instead of replacing it with a bare no_page stub.
    yaml_cfg_by_norm = {_normalize(m["name"]): m for m in yaml_members}
    carried: dict[str, dict] = {}
    for add in to_add:
        add_norm = _normalize(add["name"])
        for rm_name in to_remove:
            rm_norm = _normalize(rm_name)
            if not (add_norm.startswith(rm_norm) or rm_norm.startswith(add_norm)):
                continue
            old = yaml_cfg_by_norm.get(rm_norm, {})
            if old.get("type", "no_page") == "no_page":
                continue
            carried[add["name"]] = old
            print(
                f"[warn] '{rm_name}' appears renamed to '{add['name']}'; "
                f"carrying forward type={old.get('type')} config",
                file=sys.stderr,
            )
            break

    text = yaml_path.read_text()

    for name in to_remove:
        text = _remove_member_block(text, name)

    for m in sorted(to_add, key=lambda x: x["name"]):
        text = _append_member_block(text, m["name"], m["tier"], carried.get(m["name"]))

    yaml_path.write_text(text)
    return [m["name"] for m in to_add], to_remove


# Workday does word-level tokenization, not phrase search — "trusted execution"
# matches "Account Executive", "confidential container" matches account managers, etc.
# Focus on specific technical terms and precise multi-word phrases to avoid false positives.
WORKDAY_SEARCH_TERMS = [
    # Core confidential computing terms (safest)
    "confidential computing",
    "confidential compute", 
    
    # Specific technical acronyms (case-sensitive in search, filtered later)
    "TEE",
    "SGX", 
    "TDX",
    "SEV-SNP",
    
    # Precise technical phrases (lower false positive risk)
    "secure enclave",
    "trusted execution environment",
    "remote attestation",
    
    # Additional specific terms
    "confidential VM",
    "confidential container"
]


def _fetch_workday_description(detail_base: str, ext: str) -> str:
    """Fetch a single Workday job's description as plain text.

    detail_base is api_url with the trailing '/jobs' removed, e.g.
    'https://hpe.wd5.myworkdayjobs.com/wday/cxs/hpe/Jobsathpe'.
    ext is the externalPath returned by the search API, e.g.
    '/job/Grenoble-.../HPE-Labs..._1192380'.
    Returns empty string on any failure.
    """
    try:
        r = requests.get(f"{detail_base}{ext}", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return ""
        desc_html = r.json().get("jobPostingInfo", {}).get("jobDescription", "") or ""
        return html_to_text(desc_html)
    except Exception:
        return ""


def fetch_workday(api_url: str, job_base: str) -> tuple[list[dict], int, str | None]:
    # Strip the trailing '/jobs' to form the detail base URL.
    # api_url looks like '.../wday/cxs/<tenant>/<site>/jobs'; site can itself
    # be 'jobs' (e.g. Red Hat), so only strip the final path component.
    detail_base = api_url[:-5] if api_url.endswith("/jobs") else api_url

    seen = set()
    results = []
    api_failed = False
    
    for term in WORKDAY_SEARCH_TERMS:
        try:
            r = requests.post(
                api_url,
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=15,
            )
            if r.status_code == 502:
                # 502 = upstream Workday outage — mark for manual-search fallback
                api_failed = True
                break
            elif r.status_code != 200:
                continue
                
            for j in r.json().get("jobPostings", []):
                ext = j.get("externalPath", "")
                if ext in seen:
                    continue
                seen.add(ext)
                title = j.get("title", "")
                desc = _fetch_workday_description(detail_base, ext)
                results.append({
                    "title": title,
                    "url": f"{job_base}{ext}",
                    "location": j.get("locationsText", ""),
                    "dept": "",
                    # Use real description content; fall back to title-only so a
                    # genuinely-titled match still passes but bare "confidential"
                    # in a generic description no longer creates false positives.
                    "text": f"{title} {desc}" if desc else title,
                })
        except Exception:
            continue
    
    # A 502 from the Workday tenant (e.g. Samsung) is an upstream outage, not
    # an empty result set — surface it as an error so the report tells the
    # reader to search manually rather than implying zero CC roles.
    if api_failed and not results:
        return [], 0, "API unavailable, manual search recommended"
    
    return results, len(results), None


def fetch_teamtailor_rss(rss_url: str, cs_patterns, ci_patterns) -> tuple[list[dict], int, str | None]:
    try:
        r = requests.get(rss_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return [], 0, str(e)
    feed = feedparser.parse(r.text)
    all_jobs = []
    for entry in feed.entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        all_jobs.append({
            "title": title,
            "url": entry.get("link", ""),
            "location": "",
            "dept": "",
            "text": f"{title} {summary}",
        })
    matched = [j for j in all_jobs if matches(j["text"], cs_patterns, ci_patterns)]
    return matched, len(all_jobs), None


def fetch_herp(slug: str, cs_patterns, ci_patterns) -> tuple[list[dict], str | None]:
    url = f"https://herp.careers/v1/{slug}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    soup = BeautifulSoup(r.text, "lxml")
    results = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if f"/v1/{slug}/" in href and title:
            full_url = href if href.startswith("http") else f"https://herp.careers{href}"
            results.append({
                "title": title,
                "url": full_url,
                "location": "",
                "dept": "",
                "text": title,
            })
    matched = [j for j in results if matches(j["text"], cs_patterns, ci_patterns)]
    return matched, None


def fetch_page(url: str, cs_patterns, ci_patterns) -> tuple[bool, str | None]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return False, str(e)
    text = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True)
    return matches(text, cs_patterns, ci_patterns), None


def fetch_amd(api_url: str, job_url_base: str) -> tuple[list[dict], int, str | None]:
    """Fetch AMD jobs via their Jibe Search 4 JSON API (/api/jobs).

    The careers portal is an Angular SPA but exposes a public paginated JSON
    API with full descriptions inline.  Pagination uses page= (not offset=).
    We deduplicate by slug in case the sort order shifts between pages.
    """
    seen: set[str] = set()
    results = []
    page = 1
    total: int | None = None
    err = None
    while True:
        try:
            r = requests.get(
                api_url,
                params={"limit": 100, "page": page},
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            data = r.json()
        except Exception as e:
            err = str(e)
            break
        if total is None:
            total = data.get("totalCount", 0)
        for job in data.get("jobs", []):
            j = job.get("data", job)
            slug = str(j.get("slug", ""))
            if not slug or slug in seen:
                continue
            seen.add(slug)
            title = j.get("title", "")
            desc = html_to_text(j.get("description", "") or "")
            location = ", ".join(filter(None, [
                j.get("city", ""), j.get("state", ""), j.get("country", ""),
            ]))
            results.append({
                "title": title,
                "url": f"{job_url_base}/{slug}?lang=en-us",
                "location": location,
                "dept": "",
                "text": f"{title} {desc}",
            })
        if len(seen) >= (total or 0):
            break
        page += 1
    return results, total or len(results), err


# The Huawei portal uses separate listing views for the EU careers site and the
# global portal5 site; jobType=3 is the broadest category on each (union of
# experienced + campus).  We union both views so neither set is missed.
_HUAWEI_SCOPES = [
    {
        "referer": "https://career.huawei.com/reccampportal/euportal/portal/social-recruitment.html",
        "params": {"dataSource": 1, "jobType": 3, "locale": "en_US"},
    },
    {
        "referer": "https://career.huawei.com/reccampportal/portal5/social-recruitment.html",
        "params": {"dataSource": 1, "jobType": 3, "locale": "en_US"},
    },
]


def fetch_huawei(api_base: str, job_url_base: str) -> tuple[list[dict], int, str | None]:
    """Fetch Huawei social-recruitment jobs via their public JSON API.

    The portal is JS-rendered but backed by unauthenticated GET endpoints that
    only require a plausible Referer header.  We scan two overlapping listing
    scopes (EU portal and global portal5), deduplicate by jobId, then fetch
    each job's detail page for the full description text.
    """
    list_url = f"{api_base}/getJob/newHr/page"
    detail_url = f"{api_base}/getJobDetail/newHr"

    seen: dict[int, dict] = {}  # jobId → raw listing item

    for scope in _HUAWEI_SCOPES:
        hdrs = {**HEADERS, "Referer": scope["referer"]}
        page = 1
        while True:
            try:
                r = requests.get(
                    f"{list_url}/50/{page}",
                    params=scope["params"],
                    headers=hdrs,
                    timeout=15,
                )
                if r.status_code != 200:
                    break
                data = r.json()
            except Exception:
                break
            for item in data.get("result", []):
                jid = item.get("jobId")
                if jid and jid not in seen:
                    seen[jid] = item
            pvo = data.get("pageVO", {})
            if page >= pvo.get("totalPages", 1):
                break
            page += 1

    total = len(seen)
    results = []
    # Use either referer for detail calls — the endpoint doesn't care which
    detail_hdrs = {**HEADERS, "Referer": _HUAWEI_SCOPES[0]["referer"]}
    for jid, item in seen.items():
        title = item.get("jobname", "")
        location = (item.get("jobAddress") or "").replace("\\", " / ")
        url = f"{job_url_base}?jobId={jid}&dataSource=1"
        desc = ""
        try:
            dr = requests.get(
                detail_url,
                params={"jobId": jid, "dataSource": 1},
                headers=detail_hdrs,
                timeout=15,
            )
            if dr.status_code == 200:
                dj = dr.json()
                raw = " ".join(filter(None, [
                    dj.get("mainBusinessEn") or "",
                    dj.get("jobRequireEn") or "",
                ]))
                desc = html_to_text(raw) if "<" in raw else raw
        except Exception:
            pass
        results.append({
            "title": title,
            "url": url,
            "location": location,
            "dept": "",
            "text": f"{title} {desc}",
        })

    return results, total, None


def fetch_talentbrew(base_url: str) -> tuple[list[dict], int, str | None]:
    """Fetch jobs from a Radancy TalentBrew careers site (e.g. careers.arm.com).

    TalentBrew keyword search is fuzzy (a quoted "confidential computing"
    returns STA and SoC roles that only mention 'confidentiality'), so we
    don't use it.  Instead we page through the full listing via the
    /search-jobs/results AJAX endpoint, then fetch each job's detail page for
    the description (div.ats-description), as fetch_huawei does.
    """
    list_url = f"{base_url}/search-jobs/results"
    hdrs = {**HEADERS, "X-Requested-With": "XMLHttpRequest"}
    seen: dict[str, dict] = {}  # href → {title, location}
    page, pages = 1, 1
    err = None
    while page <= pages:
        try:
            r = requests.get(
                list_url,
                params={"CurrentPage": page, "RecordsPerPage": 100, "Keywords": "",
                        "SearchResultsModuleName": "Search Results", "SearchType": 5},
                headers=hdrs,
                timeout=30,
            )
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            soup = BeautifulSoup(r.json().get("results", ""), "lxml")
        except Exception as e:
            err = str(e)
            break
        sec = soup.select_one("section#search-results")
        if sec and sec.get("data-total-pages", "").isdigit():
            pages = int(sec["data-total-pages"])
        for li in soup.select("li.job-card"):
            a = li.select_one('a[href^="/job/"]')
            if not a or a["href"] in seen:
                continue
            loc = li.select_one(".location")
            seen[a["href"]] = {
                "title": a.get_text(" ", strip=True),
                "location": loc.get_text(" ", strip=True) if loc else "",
            }
        page += 1

    if err and not seen:
        return [], 0, err

    results = []
    for href, item in seen.items():
        url = f"{base_url}{href}"
        desc = ""
        try:
            dr = requests.get(url, headers=HEADERS, timeout=15)
            if dr.status_code == 200:
                d = BeautifulSoup(dr.text, "lxml").select_one("div.ats-description")
                desc = d.get_text(" ", strip=True) if d else ""
        except Exception:
            pass
        results.append({
            "title": item["title"],
            "url": url,
            "location": item["location"],
            "dept": "",
            "text": f"{item['title']} {desc}",
        })
    return results, len(results), None


def fetch_oracle_hcm(host: str, site: str) -> tuple[list[dict], int, str | None]:
    """Fetch jobs from an Oracle Cloud HCM candidate site (e.g. JPMorgan CX_1001).

    The candidate UI is a JS app over unauthenticated REST resources.  Sites
    like JPMorgan's are too large to page in full, so as with Workday we run
    one keyword search per term, then fetch each hit's details for the
    description.  The search is relevance-ranked and loose, so the caller's
    keyword matching does the real filtering.
    """
    api = f"https://{host}/hcmRestApi/resources/latest"
    job_base = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job"
    seen: dict[str, dict] = {}  # requisition Id → listing item
    failures = 0
    for term in WORKDAY_SEARCH_TERMS:
        finder = f'findReqs;siteNumber={site},limit=25,keyword="{term}",sortBy=RELEVANCY'
        try:
            r = requests.get(
                f"{api}/recruitingCEJobRequisitions",
                params={"onlyData": "true", "expand": "requisitionList", "finder": finder},
                headers=HEADERS,
                timeout=20,
            )
            if r.status_code != 200:
                failures += 1
                continue
            items = r.json().get("items", [])
        except Exception:
            failures += 1
            continue
        for req in (items[0].get("requisitionList", []) if items else []):
            rid = str(req.get("Id", ""))
            if rid and rid not in seen:
                seen[rid] = req
    if failures == len(WORKDAY_SEARCH_TERMS):
        return [], 0, "API unavailable, manual search recommended"

    results = []
    for rid, req in seen.items():
        title = req.get("Title", "")
        desc = ""
        try:
            dr = requests.get(
                f"{api}/recruitingCEJobRequisitionDetails",
                params={"onlyData": "true", "expand": "all",
                        "finder": f'ById;Id="{rid}",siteNumber={site}'},
                headers=HEADERS,
                timeout=15,
            )
            if dr.status_code == 200:
                ditems = dr.json().get("items", [])
                if ditems:
                    d = ditems[0]
                    # Job-specific text only; CorporateDescriptionStr is
                    # shared boilerplate.
                    raw = " ".join(filter(None, [
                        d.get("ExternalDescriptionStr") or "",
                        d.get("ExternalResponsibilitiesStr") or "",
                        d.get("ExternalQualificationsStr") or "",
                    ]))
                    desc = html_to_text(raw) if "<" in raw else raw
        except Exception:
            pass
        results.append({
            "title": title,
            "url": f"{job_base}/{rid}",
            "location": req.get("PrimaryLocation", ""),
            "dept": "",
            "text": f"{title} {desc}" if desc else title,
        })
    return results, len(results), None


def fetch_phenom(site_url: str) -> tuple[list[dict], int, str | None]:
    """Fetch jobs from a Phenom People careers site (e.g. careers.mitre.org/us/en).

    The JS front end loads results from a public POST /widgets endpoint
    (ddoKey 'refineSearch').  Its paging is unstable (pages of 100 overlap and
    skip jobs from one request to the next), so we ask for 500 at a time,
    which returns MITRE's whole listing in one response.
    Listings carry only a teaser, and Phenom job pages are ~900 KB each, so
    when a job's applyUrl points at Workday (as MITRE's do) we fetch the
    description from that Workday tenant's CXS detail endpoint instead.
    MITRE's Workday search API itself returns 0 jobs, hence going via Phenom.
    """
    parsed = urlparse(site_url)
    country, lang = (parsed.path.strip("/").split("/") + ["us", "en"])[:2]
    widgets = f"{parsed.scheme}://{parsed.netloc}/widgets"
    seen: dict[str, dict] = {}  # jobId → listing item
    start, total = 0, None
    err = None
    while total is None or start < total:
        try:
            r = requests.post(
                widgets,
                json={"lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
                      "pageName": "search-results", "ddoKey": "refineSearch",
                      "from": start, "size": 500, "jobs": True, "counts": False,
                      "keywords": "", "global": True, "selected_fields": {},
                      "siteType": "external", "sortBy": "", "subsearch": ""},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=30,
            )
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                break
            rs = r.json().get("refineSearch", {})
        except Exception as e:
            err = str(e)
            break
        jobs = (rs.get("data") or {}).get("jobs", [])
        total = rs.get("totalHits", 0)
        for j in jobs:
            jid = j.get("jobId")
            if jid and jid not in seen:
                seen[jid] = j
        if not jobs:
            break
        start += len(jobs)

    if err and not seen:
        return [], 0, err

    results = []
    for jid, j in seen.items():
        title = j.get("title", "")
        desc = ""
        apply_url = j.get("applyUrl", "") or ""
        m = re.match(r"https://([^/]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+?)(?:/apply)?/?$", apply_url)
        if m:
            host, site, ext = m.groups()
            tenant = host.split(".")[0]
            desc = _fetch_workday_description(f"https://{host}/wday/cxs/{tenant}/{site}", ext)
        if not desc:
            desc = j.get("descriptionTeaser", "") or ""
        results.append({
            "title": title,
            "url": f"{site_url.rstrip('/')}/job/{jid}",
            "location": j.get("location", "") or j.get("cityState", ""),
            "dept": "",
            "text": f"{title} {desc}" if desc else title,
        })
    return results, len(results), None


def fetch_bytedance(search_url: str, website_path: str, referer: str, job_url_base: str,
                    keyword: str = "", csrf: bool = False) -> tuple[list[dict], int, str | None]:
    """Fetch jobs from a ByteDance ATSX portal (lifeattiktok.com, jobs.bytedance.com).

    Both sites' JS front ends POST a JSON search to `search_url` (TikTok's is
    under /api/v1/public/supplier/, ByteDance's directly under /api/v1/), with
    a `website-path` header naming the site ("tiktok", or "society" for the
    ByteDance experienced-hire site, read from its js-websiteInfo blob).
    Listings inline the full description and requirements, so one pass of
    500-job pages is enough.  jobs.bytedance.com also wants a CSRF token from
    /api/v1/csrf/token on the same host, echoed back as x-csrf-token.
    Search is a loose word match ("trusted execution" returns sales roles), so
    `keyword` is only used to scope the listing (e.g. 火山引擎 for Volcano
    Engine's roles among ByteDance's), and results that don't contain it are
    dropped; CC matching happens locally.
    """
    s = requests.Session()
    s.headers.update({**HEADERS, "Content-Type": "application/json", "Accept": "application/json",
                      "Referer": referer, "Origin": f"{urlparse(referer).scheme}://{urlparse(referer).netloc}",
                      "portal-channel": "office", "portal-platform": "pc",
                      "website-path": website_path})
    try:
        if csrf:
            api = urlparse(search_url)
            r = s.post(f"{api.scheme}://{api.netloc}/api/v1/csrf/token", json={"portal_entrance": 1}, timeout=30)
            r.raise_for_status()
            s.headers["x-csrf-token"] = requests.utils.unquote(s.cookies.get("atsx-csrf-token", ""))
    except Exception as e:
        return [], 0, f"CSRF token: {e}"

    seen: dict[str, dict] = {}
    offset, total = 0, None
    err = None
    while total is None or offset < total:
        try:
            r = s.post(
                search_url,
                json={"keyword": keyword, "limit": 500, "offset": offset,
                      "job_category_id_list": [], "location_code_list": [],
                      "subject_id_list": [], "recruitment_id_list": [],
                      "job_function_id_list": [], "portal_type": 2, "portal_entrance": 1},
                timeout=60,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            err = str(e)
            break
        if body.get("code") != 0:
            err = body.get("message") or f"code {body.get('code')}"
            break
        data = body.get("data") or {}
        posts = data.get("job_post_list") or []
        total = data.get("count", 0)
        for p in posts:
            if p.get("id"):
                seen.setdefault(p["id"], p)
        if not posts:
            break
        offset += len(posts)

    if err and not seen:
        return [], 0, err

    def _name(node):
        return (node or {}).get("en_name") or (node or {}).get("name") or ""

    results = []
    for jid, p in seen.items():
        title = p.get("title", "")
        cities = p.get("city_list") or ([p["city_info"]] if p.get("city_info") else [])
        dept = _name(p.get("job_category"))
        text = f"{title} {dept} {p.get('description') or ''} {p.get('requirement') or ''}"
        if keyword and keyword not in text:
            continue
        results.append({
            "title": title,
            "url": job_url_base.format(id=jid),
            "location": ", ".join(_name(c) for c in cities),
            "dept": dept,
            "text": text,
        })
    return results, len(results), None


# Eightfold PCSX search returns a fixed 10 results per page and is semantic
# (a query matches thousands of loosely related roles), so we read only the
# top few pages per term; relevance ranking puts genuine hits first.
_EIGHTFOLD_PAGES_PER_TERM = 3


def _eightfold_get(url: str, params: dict, timeout: int) -> requests.Response:
    """GET that waits out Eightfold's HTTP 429 rate limit.

    A full Qualcomm run is ~200 requests, enough to trip the limiter; without
    retries the throttled detail calls silently degrade jobs to title-only.
    """
    for attempt in range(5):
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code != 429:
            return r
        retry_after = r.headers.get("Retry-After", "")
        time.sleep(int(retry_after) if retry_after.isdigit() else 5 * (attempt + 1))
    return r


def fetch_eightfold(host: str, domain: str) -> tuple[list[dict], int, str | None]:
    """Fetch jobs from an Eightfold AI 'PCSX' careers site (e.g. Qualcomm).

    The older /api/apply/v2/jobs endpoint now answers 'Not authorized for
    PCSX'; the public replacements are /api/pcsx/search and
    /api/pcsx/position_details.  Qualcomm lists ~2,000 roles, too many to
    page in full, so we search per term and fetch each hit's description.
    """
    api = f"https://{host}/api/pcsx"
    seen: dict[int, dict] = {}  # position id → listing item
    failures = 0
    for term in WORKDAY_SEARCH_TERMS:
        for page in range(_EIGHTFOLD_PAGES_PER_TERM):
            try:
                r = _eightfold_get(
                    f"{api}/search",
                    {"domain": domain, "query": term, "location": "", "start": page * 10},
                    timeout=20,
                )
                if r.status_code != 200:
                    if page == 0:
                        failures += 1
                    break
                positions = (r.json().get("data") or {}).get("positions", [])
            except Exception:
                if page == 0:
                    failures += 1
                break
            for pos in positions:
                pid = pos.get("id")
                if pid and pid not in seen:
                    seen[pid] = pos
            if len(positions) < 10:
                break
    if failures == len(WORKDAY_SEARCH_TERMS):
        return [], 0, "API unavailable, manual search recommended"

    results = []
    for pid, pos in seen.items():
        title = pos.get("name", "")
        desc = ""
        try:
            dr = _eightfold_get(
                f"{api}/position_details",
                {"position_id": pid, "domain": domain, "hl": "en"},
                timeout=15,
            )
            if dr.status_code == 200:
                desc = html_to_text((dr.json().get("data") or {}).get("jobDescription", "") or "")
        except Exception:
            pass
        locs = pos.get("locations") or []
        results.append({
            "title": title,
            "url": f"https://{host}{pos.get('positionUrl', f'/careers/job/{pid}')}",
            "location": locs[0] if len(locs) == 1 else f"{len(locs)} Locations" if locs else "",
            "dept": pos.get("department", "") or "",
            "text": f"{title} {desc}" if desc else title,
        })
    return results, len(results), None


# ── Member processing ─────────────────────────────────────────────────────────

def process_member(member: dict, cs_patterns, ci_patterns) -> dict:
    name = member["name"]
    mtype = member.get("type", "no_page")
    result = {
        "name": name,
        "tier": member.get("tier", "General"),
        "type": mtype,
        "jobs": [],
        "total": None,
        "note": member.get("note", ""),
        "careers_url": member.get("careers_url", ""),
        "error": None,
    }

    if mtype == "greenhouse":
        jobs, err = fetch_greenhouse(member["slug"])
        result["error"] = err
        result["total"] = len(jobs)
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "lever":
        jobs, err = fetch_lever(member["slug"])
        result["error"] = err
        result["total"] = len(jobs)
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "workday":
        jobs, total, err = fetch_workday(member["workday_api"], member["workday_job_base"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "teamtailor_rss":
        jobs, total, err = fetch_teamtailor_rss(member["rss_url"], cs_patterns, ci_patterns)
        result["error"] = err
        result["total"] = total
        result["jobs"] = jobs

    elif mtype == "herp":
        jobs, err = fetch_herp(member["slug"], cs_patterns, ci_patterns)
        result["error"] = err
        result["jobs"] = jobs

    elif mtype == "amd":
        jobs, total, err = fetch_amd(member["amd_api_url"], member["amd_job_url_base"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "huawei":
        jobs, total, err = fetch_huawei(member["huawei_api_base"], member["huawei_job_url"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "talentbrew":
        jobs, total, err = fetch_talentbrew(member["talentbrew_base"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "oracle_hcm":
        jobs, total, err = fetch_oracle_hcm(member["oracle_host"], member["oracle_site"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "eightfold":
        jobs, total, err = fetch_eightfold(member["eightfold_host"], member["eightfold_domain"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "phenom":
        jobs, total, err = fetch_phenom(member["phenom_site"])
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "bytedance":
        jobs, total, err = fetch_bytedance(
            member["bytedance_search_url"], member["bytedance_website_path"],
            member["careers_url"], member["bytedance_job_url"],
            member.get("bytedance_keyword", ""), member.get("bytedance_csrf", False))
        result["error"] = err
        result["total"] = total
        result["jobs"] = [j for j in jobs if matches(j["text"], cs_patterns, ci_patterns)]

    elif mtype == "fetch":
        found, err = fetch_page(member["careers_url"], cs_patterns, ci_patterns)
        result["error"] = err
        result["page_has_keywords"] = found

    # gated / blocked / no_page: nothing to fetch
    elif mtype not in ("gated", "blocked", "no_page"):
        # An unrecognised type would otherwise be skipped silently and vanish
        # from the report, hiding the member entirely. Surface it loudly.
        result["error"] = f"unknown member type '{mtype}' — not crawled"
        print(f"[warn] {member['name']}: unknown type '{mtype}' — not crawled",
              file=sys.stderr)

    return result


# ── Report rendering ──────────────────────────────────────────────────────────

def _job_line(j: dict) -> str:
    parts = []
    if j.get("dept"):
        parts.append(j["dept"])
    if j.get("location"):
        parts.append(j["location"])
    suffix = "; ".join(parts)
    return f"- [{j['title']}]({j['url']})" + (f" — {suffix}" if suffix else "")


def _member_block(r: dict) -> list[str]:
    lines = [f"#### {r['name']}", ""]
    mtype = r["type"]
    jobs = r.get("jobs", [])
    error = r.get("error")
    careers_url = r.get("careers_url", "")
    note = r.get("note", "")
    total = r.get("total")

    if error:
        lines.append(f"> Fetch error: {error}")
    elif jobs:
        for j in jobs:
            lines.append(_job_line(j))
    else:
        if mtype in ("greenhouse", "lever"):
            lines.append(f"> Board active ({total} total roles) — no CC-specific roles found.")
        elif mtype == "workday":
            lines.append(f"> Workday portal searched — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "amd":
            lines.append(f"> AMD portal searched ({total} total roles) — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "huawei":
            lines.append(f"> Huawei portal searched ({total} total roles) — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "talentbrew":
            lines.append(f"> Careers portal searched ({total} total roles) — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "oracle_hcm":
            lines.append(f"> Oracle HCM portal searched — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "eightfold":
            lines.append(f"> Eightfold portal searched — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "phenom":
            lines.append(f"> Phenom portal searched ({total} total roles) — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "bytedance":
            lines.append(f"> ByteDance portal searched ({total} total roles) — no CC roles found. Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "teamtailor_rss":
            count = f"{total} open roles" if total else "Active"
            lines.append(f"> {count} at [{careers_url}]({careers_url}) — none match CC keywords.")
        elif mtype == "herp":
            lines.append(f"> Herp board active — no CC roles matched. {note} Visit: [{careers_url}]({careers_url}).")
        elif mtype == "fetch":
            if r.get("page_has_keywords"):
                suffix = f" {note}" if note else ""
                lines.append(f"> CC keywords found on page — visit directly at [{careers_url}]({careers_url}).{suffix}")
            else:
                suffix = f" {note.rstrip('.')}." if note else ""
                lines.append(f"> No CC keywords found.{suffix} Visit: [{careers_url}]({careers_url}).")
        elif mtype == "gated":
            note_part = f" {note.rstrip('.')}." if note else ""
            lines.append(f"> JS-rendered or gated portal — not crawlable.{note_part} Search directly at [{careers_url}]({careers_url}).")
        elif mtype == "blocked":
            lines.append(f"> Careers page blocked ({note}). Try directly: [{careers_url}]({careers_url}).")

    lines.append("")
    return lines


def render_report(results: list[dict], date_str: str, diff: dict | None = None) -> str:
    kw_str = "confidential computing, confidential compute, enclave, TEE, SGX, TDX, SEV, SEV-SNP, Arm CCA, remote attestation, confidential VM, confidential container, trusted execution"
    lines = [
        "# CCC Member Jobs — Confidential Computing",
        "",
        f"**Date:** {date_str}",
        f"**Keywords:** {kw_str}",
        "**Search method:** Greenhouse/Lever APIs (full job content); Workday search API; Teamtailor RSS; Herp HTML; Huawei JSON API; direct page fetch where available.",
        "**Coverage note:** JS-rendered portals (Intel, Microsoft, Arm, Google, Meta, TikTok, Qualcomm) not crawlable — search directly.",
        "",
        "---",
        "",
    ]

    if diff is not None:
        lines += render_diff_section(diff)

    open_members = [r for r in results if r.get("jobs")]
    total_open = sum(len(r["jobs"]) for r in open_members)
    lines += [f"**Open roles:** {total_open}", ""]
    lines += ["## Open Roles", ""]
    if not open_members:
        lines += ["> No matching open roles in this run.", ""]
    else:
        for r in open_members:
            lines.append(f"### {r['name']}")
            lines.append("")
            for j in r["jobs"]:
                lines.append(_job_line(j))
            lines.append("")
    lines += ["---", ""]

    crawlable_types = {"greenhouse", "lever", "workday", "teamtailor_rss", "herp", "fetch", "amd", "huawei", "talentbrew", "oracle_hcm", "eightfold", "phenom", "bytedance", "gated", "blocked"}
    lines += ["## Coverage by Tier", ""]
    for tier, heading in [("Premier", "Premier Members"), ("General", "General Members"), ("Startup", "Startup Members"), ("Associate", "Associate Members")]:
        tier_results = [r for r in results if r["tier"] == tier and r["type"] in crawlable_types]
        if not tier_results:
            continue
        lines += [f"### {heading}", ""]
        for r in tier_results:
            lines += _member_block(r)
    lines += ["---", ""]

    # No-page summary
    no_page = [r for r in results if r["type"] == "no_page"]
    if no_page:
        lines += ["## Members with no accessible careers page", ""]
        for r in no_page:
            note = r.get("note", "No accessible page")
            lines.append(f"- **{r['name']}** ({r['tier']}): {note}")
        lines.append("")

    return "\n".join(lines)


# ── Snapshot ─────────────────────────────────────────────────────────────────

def write_snapshot(results: list[dict], date_str: str, path: Path) -> None:
    members = []
    for r in results:
        members.append({
            "name": r["name"],
            "tier": r["tier"],
            "type": r["type"],
            "error": r.get("error"),
            "total": r.get("total"),
            "jobs": [
                {"title": j["title"], "url": j["url"],
                 "location": j.get("location", ""), "dept": j.get("dept", "")}
                for j in r.get("jobs", [])
            ],
        })
    path.write_text(json.dumps({"date": date_str, "members": members}, indent=2))


def find_previous_snapshot(report_dir: Path, today: str) -> dict | None:
    candidates = sorted(report_dir.glob("CCC-jobs-????-??-??.json"))
    for p in reversed(candidates):
        stem_date = p.stem.replace("CCC-jobs-", "")
        if stem_date < today:
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


def compute_diff(prev: dict, curr_results: list[dict]) -> dict:
    prev_by_name = {m["name"]: m for m in prev.get("members", [])}
    curr_by_name = {r["name"]: r for r in curr_results}

    new_jobs = []
    closed_jobs = []

    all_names = set(prev_by_name) | set(curr_by_name)
    for name in all_names:
        prev_member = prev_by_name.get(name)
        curr_member = curr_by_name.get(name)

        # Can't diff if no data on either side, or if this run errored
        if not prev_member or not curr_member:
            continue
        if curr_member.get("error"):
            continue

        prev_urls = {j["url"]: j for j in prev_member.get("jobs", [])}
        curr_urls = {j["url"]: j for j in curr_member.get("jobs", [])}

        for url, job in curr_urls.items():
            if url not in prev_urls:
                new_jobs.append({"member": name, **job})

        for url, job in prev_urls.items():
            if url not in curr_urls:
                closed_jobs.append({"member": name, **job})

    return {"prev_date": prev["date"], "new_jobs": new_jobs, "closed_jobs": closed_jobs}


def render_diff_section(diff: dict) -> list[str]:
    prev = diff["prev_date"]
    n, c = len(diff["new_jobs"]), len(diff["closed_jobs"])
    if n == 0 and c == 0:
        return [f"**Changes since {prev}:** none.", ""]
    return [f"**Changes since {prev}:** {n} new, {c} closed.", ""]


# ── Outreach email generation ─────────────────────────────────────────────────

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Turn a company name into a safe file stem (e.g. 'DLT Research' → 'DLT_Research')."""
    cleaned = _FILENAME_UNSAFE.sub("_", name).strip("_")
    return cleaned or "company"


def write_member_emails(
    results: list[dict], template_path: str | Path, out_dir: Path, prev_snapshot: dict | None = None
) -> int:
    """Write one outreach email per member with ≥1 matched job.

    Jobs are inserted between the ``[]`` placeholder in the template.
    If prev_snapshot is provided, separates "New jobs" and "All active jobs".
    Returns the number of files written.
    """
    open_members = [r for r in results if r.get("jobs")]
    if not open_members:
        return 0

    try:
        template = Path(template_path).read_text()
    except FileNotFoundError:
        print(f"[warn] email template not found: {template_path}; skipping emails",
              file=sys.stderr)
        return 0

    # Build index of previous job URLs per member to identify new jobs
    prev_by_member = {}
    if prev_snapshot:
        for m in prev_snapshot.get("members", []):
            prev_by_member[m["name"]] = {j["url"] for j in m.get("jobs", [])}

    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for r in open_members:
        member_name = r["name"]
        curr_jobs = r["jobs"]
        
        # Format the job lines
        if prev_snapshot:
            prev_urls = prev_by_member.get(member_name, set())
            new_jobs = [j for j in curr_jobs if j["url"] not in prev_urls]
            
            job_sections = []
            
            # 1. New jobs section
            job_sections.append("New jobs:")
            if new_jobs:
                new_lines = "\n".join(f"- {j['title']}: {j['url']}" for j in new_jobs)
                job_sections.append(new_lines)
            else:
                job_sections.append("- None")
            
            job_sections.append("") # Spacer
            
            # 2. All active jobs section
            job_sections.append("All active jobs:")
            active_lines = "\n".join(f"- {j['title']}: {j['url']}" for j in curr_jobs)
            job_sections.append(active_lines)
            
            job_content = "\n".join(job_sections)
        else:
            # First run/no snapshot: list all active jobs directly
            job_sections = [
                "All active jobs:",
                "\n".join(f"- {j['title']}: {j['url']}" for j in curr_jobs)
            ]
            job_content = "\n".join(job_sections)

        body = template.replace("[]", job_content, 1)
        path = out_dir / f"{_safe_filename(member_name)}.txt"
        path.write_text(body)
        written += 1
    return written


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CCC job board crawler")
    parser.add_argument("--members", default=str(Path(__file__).parent / "members.yaml"))
    parser.add_argument("--output", default=None, help="Output .md file (default: CCC-jobs-YYYY-MM-DD.md)")
    parser.add_argument("--lfx-cache", default=str(Path(__file__).parent / "lfx-members.json"),
                        help="Path to LFX member list cache (refreshed weekly)")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-fetch CCC members from LFX even if the cache is fresh")
    parser.add_argument("--no-sync", action="store_true",
                        help="Never rewrite the --members file from LFX (use for test configs)")
    parser.add_argument("--email-template", default=None,
                        help="Outreach email template; the [] placeholder is replaced with "
                             "matched jobs (default: email-template.txt, falling back to "
                             "email-template.example.txt)")
    args = parser.parse_args()
    if args.email_template is None:
        # email-template.txt is a local, gitignored copy so each sender can
        # sign it themselves; the committed example is the fallback.
        here = Path(__file__).parent
        local = here / "email-template.txt"
        args.email_template = str(local if local.exists() else here / "email-template.example.txt")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_path = Path(args.output or Path(__file__).parent / f"CCC-jobs-{date_str}.md")

    with open(args.members) as f:
        config = yaml.safe_load(f)

    cs_patterns, ci_patterns = build_matchers(config.get("keywords", {}))
    members = config.get("members", [])

    # ── LFX membership drift check ────────────────────────────────────────────
    lfx_cache = Path(args.lfx_cache)
    try:
        cache_age = time.time() - os.path.getmtime(lfx_cache)
    except FileNotFoundError:
        cache_age = None

    lfx_synced = False
    if cache_age is None or cache_age > MEMBERS_TTL_SECONDS or args.force_refresh:
        print("Checking LFX for CCC member updates...", file=sys.stderr)
        try:
            lfx_members = fetch_lfx_members(lfx_cache)
            print(f"  Fetched {len(lfx_members)} members from LFX → {lfx_cache}", file=sys.stderr)
            lfx_synced = True
        except Exception as e:
            lfx_members = load_lfx_members(lfx_cache)
            if lfx_members is not None:
                age_days = int((cache_age or 0) // 86400)
                print(f"[warn] LFX refresh failed ({e}); using stale cache ({age_days}d old)", file=sys.stderr)
            else:
                print(f"[warn] LFX refresh failed ({e}); skipping membership sync", file=sys.stderr)
    else:
        lfx_members = load_lfx_members(lfx_cache)

    if lfx_members and lfx_synced and args.no_sync:
        print("  [--no-sync] skipping members.yaml rewrite", file=sys.stderr)
    elif lfx_members and lfx_synced:
        added, removed = sync_members_yaml(lfx_members, Path(args.members), members)
        if added:
            print(f"  Added {len(added)} new member(s) to {args.members}:", file=sys.stderr)
            for n in added:
                print(f"    + {n}", file=sys.stderr)
        if removed:
            print(f"  Removed {len(removed)} departed member(s) from {args.members}:", file=sys.stderr)
            for n in removed:
                print(f"    - {n}", file=sys.stderr)
        if added or removed:
            # Reload so the crawl uses the updated member list
            with open(args.members) as f:
                members = yaml.safe_load(f).get("members", [])
    results = []

    for i, member in enumerate(members, 1):
        name = member["name"]
        mtype = member.get("type", "no_page")
        print(f"[{i:2d}/{len(members)}] {name} ({mtype})...", end=" ", flush=True)
        r = process_member(member, cs_patterns, ci_patterns)
        count = len(r.get("jobs", []))
        if r.get("error"):
            print(f"ERROR: {r['error']}")
        elif mtype == "no_page":
            print("skipped")
        elif count:
            print(f"{count} CC job(s)")
        else:
            print("—")
        results.append(r)

    snapshot_path = output_path.with_suffix(".json")
    prev_snapshot = find_previous_snapshot(output_path.parent, date_str)
    diff = compute_diff(prev_snapshot, results) if prev_snapshot else None

    report = render_report(results, date_str, diff)
    output_path.write_text(report)
    write_snapshot(results, date_str, snapshot_path)
    print(f"\nReport written to {output_path}")
    print(f"Snapshot written to {snapshot_path}")

    emails_dir = output_path.parent / f"emails-{date_str}"
    n_emails = write_member_emails(results, args.email_template, emails_dir, prev_snapshot)
    if n_emails:
        print(f"Wrote {n_emails} outreach email(s) to {emails_dir}")

    if diff:
        print(f"Changes since {diff['prev_date']}: {len(diff['new_jobs'])} new, {len(diff['closed_jobs'])} closed")

    cc_count = sum(len(r.get("jobs", [])) for r in results)
    print(f"Total CC roles found: {cc_count}")


if __name__ == "__main__":
    main()
