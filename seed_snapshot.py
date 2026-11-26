#!/usr/bin/env python3
"""One-shot: parse a crawler-generated CCC-jobs markdown report into a JSON snapshot.

Usage: python3 seed_snapshot.py CCC-jobs-2026-05-12.md
Produces: CCC-jobs-2026-05-12.json in the same directory.
"""

import json
import re
import sys
from pathlib import Path


JOB_RE = re.compile(r"^\s*-\s+\[([^\]]+)\]\(([^)]+)\)(?:\s+—\s+(.*))?$")
MEMBER_RE = re.compile(r"^###\s+(.+)$")
DATE_RE = re.compile(r"^\*\*Date:\*\*\s+(\d{4}-\d{2}-\d{2})")
TIER_HEADING_RE = re.compile(r"^##\s+(Premier|General|Associate)")


def parse_report(text: str) -> dict:
    date_str = ""
    members = []
    current_member = None
    current_tier = "General"

    for line in text.splitlines():
        m = DATE_RE.match(line)
        if m:
            date_str = m.group(1)
            continue

        m = TIER_HEADING_RE.match(line)
        if m:
            current_tier = m.group(1)
            continue

        m = MEMBER_RE.match(line)
        if m:
            if current_member is not None:
                members.append(current_member)
            current_member = {"name": m.group(1), "tier": current_tier,
                              "type": "unknown", "error": None, "total": None, "jobs": []}
            continue

        if current_member is None:
            continue

        m = JOB_RE.match(line)
        if m:
            title = m.group(1)
            url = m.group(2)
            suffix = m.group(3) or ""
            # suffix is "dept; location" or "location" or just dept — split on first "; "
            parts = [p.strip() for p in suffix.split(";", 1)] if suffix else []
            dept = parts[0] if len(parts) > 0 else ""
            location = parts[1] if len(parts) > 1 else ""
            current_member["jobs"].append({"title": title, "url": url,
                                           "location": location, "dept": dept})

    if current_member is not None:
        members.append(current_member)

    return {"date": date_str, "members": members}


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <CCC-jobs-YYYY-MM-DD.md>", file=sys.stderr)
        sys.exit(1)

    md_path = Path(sys.argv[1])
    if not md_path.exists():
        print(f"File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    snapshot = parse_report(md_path.read_text())
    if not snapshot["date"]:
        snapshot["date"] = md_path.stem.replace("CCC-jobs-", "")

    out_path = md_path.with_suffix(".json")
    out_path.write_text(json.dumps(snapshot, indent=2))

    total_jobs = sum(len(m["jobs"]) for m in snapshot["members"])
    print(f"Seeded {out_path}: {len(snapshot['members'])} members, {total_jobs} jobs, date={snapshot['date']}")


if __name__ == "__main__":
    main()
