#!/usr/bin/env python3
"""Gate a pip-audit JSON report against the triage list (Sprint 8, T8.6).

Usage: check_pip_audit.py <report.json> <ignore-file>

Exit 0 when every reported advisory is either absent or explicitly triaged in
the ignore file; exit 1 (with the offending advisories printed) otherwise.
pip-audit has no severity filter, so the gate is "known vulnerability,
untriaged" — stricter than a severity threshold, and it forces a named owner
and a review date instead of a silent cutoff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def triaged_ids(ignore_file: Path) -> set[str]:
    if not ignore_file.exists():
        return set()
    return {stripped for line in ignore_file.read_text().splitlines() if (stripped := line.split("#", 1)[0].strip())}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <report.json> <ignore-file>", file=sys.stderr)
        return 2

    report_path, ignore_file = Path(argv[1]), Path(argv[2])
    try:
        report = json.loads(report_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read pip-audit report {report_path}: {e}", file=sys.stderr)
        return 1

    ignored = triaged_ids(ignore_file)
    untriaged: list[str] = []
    for dep in report.get("dependencies", []):
        name, version = dep.get("name", "?"), dep.get("version", "")
        for vuln in dep.get("vulns", []):
            vuln_id = vuln.get("id", "")
            if {vuln_id, *vuln.get("aliases", [])} & ignored:
                print(f"  [triaged] {name} {version}: {vuln_id}")
                continue
            fix = ", ".join(vuln.get("fix_versions", [])) or "no fix released"
            untriaged.append(f"  {name} {version}: {vuln_id} (fix: {fix})")

    if untriaged:
        print("\nUntriaged vulnerabilities:")
        print("\n".join(untriaged))
        print(f"\nUpgrade, or add the advisory id to {ignore_file} with an owner and a review date.")
        return 1

    print("  No untriaged Python vulnerabilities.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
