#!/usr/bin/env python3
"""Non-CCC-member Confidential Computing job aggregator.

Covers the market outside the consortium. Where `crawler.py` walks the CCC
member list, this walks `non_member_targets.yaml`: companies that plausibly
hire for confidential computing but aren't members.

History worth knowing: this script used to report a "total opportunities"
number in the 90s that contained zero actual jobs. Three of its four sources
were empty stubs (HackerNews and Stack Overflow were `pass`; the GitHub one
searched *repositories* and reported repos as jobs), and the remaining source,
RemoteOK, only serves the 100 most recent remote listings, which never include
CC roles. The count was made up almost entirely of pre-built LinkedIn search
URLs and hardcoded careers-page links — identical every week.

It now crawls real ATS APIs via `cc_common` (the same fetchers and keyword
matcher the member crawler uses) and reports actual postings. Search links
still exist, but in a clearly separated "manual search aids" section that does
not contribute to the job count.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from cc_common import (
    build_matchers,
    dedupe_jobs,
    fetch_board,
    matched_keywords,
    matches,
    only_weak_hits,
    WEAK_KEYWORDS,
)


_STRIP_LEGAL = re.compile(
    r"\s+(Inc\.?|LLC|Ltd\.?|Limited|Corporation|Corp\.?|Co\.?|LP|LLP|GmbH|S\.A\.)$",
    flags=re.IGNORECASE,
)


def normalize_company(name: str) -> str:
    """Normalise a company name for member comparison.

    Mirrors crawler.py's `_normalize` so "Acme Inc." and "Acme" compare equal.
    """
    name = _STRIP_LEGAL.sub("", str(name)).strip(" ,.")
    return re.sub(r"\s+", " ", name).lower()


def load_member_names(members_path: Path) -> set[str]:
    """Normalised CCC member names, used to exclude members from this report.

    If a target company joins the CCC, it must drop out of the non-member
    report rather than being counted in both. A missing members.yaml is not
    fatal — we warn and skip the check, since the aggregator is still useful
    without it.
    """
    try:
        with open(members_path) as f:
            members = yaml.safe_load(f).get("members", [])
    except (FileNotFoundError, AttributeError) as e:
        print(f"[warn] cannot read members from {members_path} ({e}); "
              "skipping member-exclusion check", file=sys.stderr)
        return set()
    return {normalize_company(m["name"]) for m in members if m.get("name")}


def load_keywords(members_path: Path) -> dict:
    """Keyword config, shared with the member crawler.

    Deliberately reads members.yaml rather than keeping a second list: when the
    two drifted, the non-member report was matching on different terms than the
    member report, making the two incomparable.
    """
    try:
        with open(members_path) as f:
            kw = yaml.safe_load(f).get("keywords")
        if kw:
            return kw
    except (FileNotFoundError, AttributeError):
        pass
    print(f"[warn] no keywords in {members_path}; falling back to defaults",
          file=sys.stderr)
    return {
        "case_sensitive": ["TEE", "SGX", "TDX", "SEV", "SEV-SNP"],
        "case_insensitive": [
            "confidential computing", "confidential compute", "enclave",
            "arm cca", "remote attestation", "confidential vm",
            "confidential container", "trusted execution",
        ],
    }


def crawl_targets(targets: list[dict], member_names: set[str],
                  cs_patterns, ci_patterns) -> tuple[list[dict], list[dict]]:
    """Crawl each target board and keep the CC-matching postings.

    Returns (jobs, board_stats). board_stats records what happened for every
    target — including zero-hit and errored boards — so a board that silently
    stops returning jobs is visible in the report instead of just vanishing.
    """
    jobs = []
    stats = []

    for t in targets:
        company = t["company"]
        if normalize_company(company) in member_names:
            print(f"  {company}: skipped — now a CCC member")
            stats.append({"company": company, "sector": t.get("sector", ""),
                          "scanned": 0, "hits": 0, "status": "skipped (CCC member)"})
            continue

        fetched, err = fetch_board(t["ats"], t["slug"])
        if err:
            print(f"  {company}: error — {err}")
            stats.append({"company": company, "sector": t.get("sector", ""),
                          "scanned": 0, "hits": 0, "status": f"error: {err}"})
            continue

        deduped = dedupe_jobs(fetched)
        hits = []
        for j in deduped:
            if matches(j["text"], cs_patterns, ci_patterns):
                kws = matched_keywords(j["text"], cs_patterns, ci_patterns)
                hits.append({
                    "source": f"{t['ats'].title()} ({company})",
                    "company": company,
                    "sector": t.get("sector", ""),
                    "title": j["title"],
                    "url": j["url"],
                    "location": j.get("location", "") or "Not specified",
                    "dept": j.get("dept", ""),
                    "keywords": kws,
                    "needs_review": only_weak_hits(kws),
                })
        n_review = sum(1 for h in hits if h["needs_review"])
        print(f"  {company}: {len(deduped)} jobs scanned, "
              f"{len(hits) - n_review} CC matches, {n_review} needing review")
        jobs.extend(hits)
        stats.append({"company": company, "sector": t.get("sector", ""),
                      "scanned": len(deduped), "hits": len(hits) - n_review,
                      "review": n_review, "status": "ok"})

    # Content-level dedupe: same title + location at one company is one role,
    # even when the board issues it two requisition IDs.
    return dedupe_jobs(jobs, by_content=True), stats


def build_manual_aids(manual: list[dict], keywords: list[str],
                      member_names: set[str]) -> list[dict]:
    """Careers-page and LinkedIn links for companies with no crawlable API.

    These are navigation aids, not findings. They are returned separately and
    never counted as jobs.
    """
    aids = []
    for m in manual:
        if normalize_company(m["company"]) in member_names:
            continue
        searches = [
            {
                "keyword": kw,
                "url": (f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(kw)}"
                        f"&f_C={quote_plus(m['company'])}&location=Worldwide&f_TPR=r604800"),
            }
            for kw in keywords
        ]
        aids.append({
            "company": m["company"],
            "sector": m.get("sector", ""),
            "careers_url": m.get("careers_url", ""),
            "linkedin_searches": searches,
        })
    return aids


def render_report(jobs: list[dict], stats: list[dict], aids: list[dict],
                  keywords: dict, date_str: str, diff: dict | None) -> str:
    total_scanned = sum(s["scanned"] for s in stats)
    ok_boards = [s for s in stats if s["status"] == "ok"]
    review = [j for j in jobs if j.get("needs_review")]
    jobs = [j for j in jobs if not j.get("needs_review")]

    def tag(j):
        return " *(needs review)*" if j.get("needs_review") else ""

    lines = [
        "# Non-Member Confidential Computing Jobs — " + date_str,
        "",
        f"**Generated:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
        f"**Open CC roles found:** {len(jobs)}"
        + (f" (plus {len(review)} needing review)" if review else ""),
        f"**Scanned:** {total_scanned} postings across {len(ok_boards)} "
        f"non-member job boards",
        "",
        "**Keywords — case-sensitive:** " + ", ".join(keywords.get("case_sensitive", [])),
        "**Keywords — case-insensitive:** " + ", ".join(keywords.get("case_insensitive", [])),
        "",
    ]

    if diff:
        lines += [f"**Changes since {diff['since']}:** {len(diff['new'])} new, "
                  f"{len(diff['closed'])} closed.", ""]

    lines += ["---", "", "## Open CC Roles", ""]

    if not jobs:
        lines += ["> No matching open roles in this run.", ""]
    else:
        by_sector = defaultdict(list)
        for j in jobs:
            by_sector[j.get("sector") or "Other"].append(j)

        for sector in sorted(by_sector):
            lines += [f"### {sector}", ""]
            by_company = defaultdict(list)
            for j in by_sector[sector]:
                by_company[j["company"]].append(j)
            for company in sorted(by_company):
                lines += [f"#### {company}", ""]
                for j in by_company[company]:
                    kw = ", ".join(f"`{k}`" for k in j["keywords"])
                    loc = j["location"]
                    lines.append(f"- **[{j['title']}]({j['url']})** — {loc}")
                    lines.append(f"  - matched: {kw}")
                lines.append("")

    if review:
        lines += [
            "---", "",
            "## Needs Review", "",
            "*These matched only weak keywords ("
            + ", ".join(f"`{k}`" for k in sorted(WEAK_KEYWORDS))
            + "), which also occur as ordinary English. Check the description "
            "before treating them as CC roles. Not included in the role count.*",
            "",
        ]
        for j in sorted(review, key=lambda x: (x["company"], x["title"])):
            kw = ", ".join(f"`{k}`" for k in j["keywords"])
            lines.append(f"- {j['company']} — **[{j['title']}]({j['url']})** "
                         f"— {j['location']}")
            lines.append(f"  - matched: {kw}")
        lines.append("")

    if diff and (diff["new"] or diff["closed"]):
        lines += ["---", "", f"## Changes since {diff['since']}", ""]
        if diff["new"]:
            lines += ["**Newly opened:**", ""]
            for j in diff["new"]:
                lines.append(f"- {j['company']} — [{j['title']}]({j['url']}){tag(j)}")
            lines.append("")
        if diff["closed"]:
            lines += ["**No longer listed:**", ""]
            for j in diff["closed"]:
                lines.append(f"- {j['company']} — {j['title']}{tag(j)}")
            lines.append("")

    lines += ["---", "", "## Board Coverage", "",
              "| Company | Sector | Postings scanned | CC roles | Needs review | Status |",
              "|---|---|---|---|---|---|"]
    for s in sorted(stats, key=lambda x: (-x["hits"], x["company"])):
        lines.append(f"| {s['company']} | {s['sector']} | {s['scanned']} | "
                     f"{s['hits']} | {s.get('review', 0)} | {s['status']} |")
    lines.append("")

    if aids:
        lines += [
            "---", "",
            "## Manual Search Aids",
            "",
            "*These companies have no crawlable public job API (JS-rendered "
            "portals, Workday behind SSO, or bespoke boards). The links below "
            "are search shortcuts, **not** confirmed openings, and are excluded "
            "from the role count above.*",
            "",
        ]
        by_sector = defaultdict(list)
        for a in aids:
            by_sector[a.get("sector") or "Other"].append(a)
        for sector in sorted(by_sector):
            lines += [f"### {sector}", ""]
            for a in by_sector[sector]:
                lines.append(f"- **{a['company']}** — [careers page]({a['careers_url']})")
                shortcuts = " · ".join(
                    f"[{s['keyword']}]({s['url']})" for s in a["linkedin_searches"]
                )
                lines.append(f"  - LinkedIn: {shortcuts}")
            lines.append("")

    lines += [
        "---", "",
        "## Notes",
        "",
        "- Roles are matched on the full job description, not just the title.",
        "- The `matched:` line shows which keywords fired, so false positives "
        "can be spotted quickly.",
        "- Known non-CC uses of CC acronyms are filtered out (for example "
        "Datadog's \"Technical Escalation Engineering (TEE)\" team and "
        "\"SEV\" meaning incident severity).",
        "- Roles matching only weak keywords (ordinary-English phrases such "
        "as \"trusted execution partner\") are listed under Needs Review and "
        "excluded from the role count.",
        "- Companies are cross-checked against `members.yaml`; any that join "
        "the CCC drop out of this report and appear in the member report "
        "instead.",
        "",
    ]
    return "\n".join(lines)


def compute_diff(jobs: list[dict], snapshot_dir: Path, today: str) -> dict | None:
    """Diff against the most recent previous snapshot, if one exists."""
    prev_files = sorted(
        p for p in snapshot_dir.glob("Non-member-CC-jobs-*.json")
        if today not in p.name
    )
    if not prev_files:
        return None
    prev_path = prev_files[-1]
    try:
        with open(prev_path) as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # Older snapshots stored search links under 'opportunities' with no real
    # jobs; treat a missing 'jobs' key as "no previous jobs" rather than
    # crashing or diffing against search URLs.
    prev_jobs = prev.get("jobs")
    if prev_jobs is None:
        return None

    def key(j):
        return j.get("url") or (j.get("company"), j.get("title"))

    prev_keys = {key(j) for j in prev_jobs}
    cur_keys = {key(j) for j in jobs}
    return {
        "since": prev.get("date", prev_path.stem),
        "new": [j for j in jobs if key(j) not in prev_keys],
        "closed": [j for j in prev_jobs if key(j) not in cur_keys],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate CC job openings at non-CCC-member companies")
    here = Path(__file__).parent
    parser.add_argument("--targets", default=str(here / "non_member_targets.yaml"),
                        help="Target company/board config")
    parser.add_argument("--members", default=str(here / "members.yaml"),
                        help="CCC member list, used for keywords and exclusion")
    parser.add_argument("--output", default=None,
                        help="Output .md path (default: Non-member-CC-jobs-YYYY-MM-DD.md)")
    parser.add_argument("--no-aids", action="store_true",
                        help="Omit the manual search aids section")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    members_path = Path(args.members)

    with open(args.targets) as f:
        cfg = yaml.safe_load(f)

    keywords = load_keywords(members_path)
    cs_patterns, ci_patterns = build_matchers(keywords)
    member_names = load_member_names(members_path)

    print(f"Crawling non-member job boards for {today}...")
    jobs, stats = crawl_targets(cfg.get("targets", []), member_names,
                                cs_patterns, ci_patterns)

    aids = [] if args.no_aids else build_manual_aids(
        cfg.get("manual_search", []),
        cfg.get("linkedin_keywords", []),
        member_names,
    )

    out_md = Path(args.output) if args.output else here / f"Non-member-CC-jobs-{today}.md"
    out_json = out_md.with_suffix(".json")

    diff = compute_diff(jobs, out_md.parent, today)

    out_md.write_text(render_report(jobs, stats, aids, keywords, today, diff))
    with open(out_json, "w") as f:
        json.dump({
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "keywords": keywords,
            "total_cc_roles": sum(1 for j in jobs if not j["needs_review"]),
            "total_needs_review": sum(1 for j in jobs if j["needs_review"]),
            "total_scanned": sum(s["scanned"] for s in stats),
            "jobs": jobs,
            "board_stats": stats,
            "manual_search_aids": aids,
        }, f, indent=2)

    print(f"\nReport written to {out_md}")
    print(f"Snapshot written to {out_json}")
    n_review = sum(1 for j in jobs if j["needs_review"])
    print(f"Total CC roles found: {len(jobs) - n_review} "
          f"(plus {n_review} needing review)")
    if diff:
        print(f"Changes since {diff['since']}: {len(diff['new'])} new, "
              f"{len(diff['closed'])} closed")

    errs = [s for s in stats if s["status"].startswith("error")]
    if errs:
        print(f"\n[warn] {len(errs)} board(s) failed:", file=sys.stderr)
        for s in errs:
            print(f"  {s['company']}: {s['status']}", file=sys.stderr)


if __name__ == "__main__":
    main()
