#!/usr/bin/env python3
"""Weekly catalog refresh — triggered by Windows Task Scheduler.
Runs catalog_health_check.py --refresh, then rebuilds catalog.duckdb index."""
import datetime
import os
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS   = os.path.join(SKILL_DIR, "scripts")
LOG_PATH  = os.path.join(SKILL_DIR, "data", "catalog_refresh.log")


def run(script, extra_args=()):
    path = os.path.join(SCRIPTS, script)
    if not os.path.exists(path):
        return f"SKIP {script} (not found)\n"
    result = subprocess.run(
        [sys.executable, path] + list(extra_args),
        cwd=SKILL_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    out = result.stdout + (result.stderr or "") + f"exit={result.returncode}\n"
    return out


def main():
    stamp = datetime.datetime.now().isoformat()
    lines = [f"\n--- {stamp} ---\n"]

    print(f"[{stamp}] Running catalog health check + refresh...")
    lines.append(run("catalog_health_check.py", ["--refresh"]))

    print("Rebuilding catalog.duckdb index...")
    lines.append(run("build_catalog_index.py"))

    lines.append("--- done ---\n")

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"Complete. Log: {LOG_PATH}")


if __name__ == "__main__":
    main()
