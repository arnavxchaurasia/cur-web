#!/usr/bin/env python3
"""
Shared logging setup for aws-gcp-cost-projection scripts.

Usage in any script:
    from log_utils import get_logger, fatal
    log = get_logger(__name__)
    log.info("doing X")
    log.warning("Y is missing, skipping")
    fatal(log, "cannot proceed without Z", phase=2)  # writes progress.json + sys.exit(1)
"""

import json
import logging
import os
import sys
import traceback

# ── log format ────────────────────────────────────────────────────────────────
_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"

_configured = False


def get_logger(name: str, job_dir: str | None = None) -> logging.Logger:
    """
    Return a logger for *name*.  On first call also wires up:
      - stdout handler at INFO (so the agent sees progress lines)
      - file handler at DEBUG in <job_dir>/projection-audit/debug.log
        (full trace for post-mortem debugging)
    """
    global _configured
    if not _configured:
        _configure(job_dir or os.getcwd())
        _configured = True
    return logging.getLogger(name)


def _configure(job_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # stdout — INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # debug.log — everything (DEBUG+)
    log_dir = os.path.join(job_dir, "projection-audit")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "debug.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("root").info(f"Log file: {log_path}")


def fatal(
    log: logging.Logger,
    message: str,
    *,
    phase: int | None = None,
    exc: BaseException | None = None,
    job_dir: str | None = None,
) -> None:
    """
    Log a CRITICAL error, mark progress.json as FAILED, and exit 1.
    Only call this when the job truly cannot continue and the frontend
    must surface a failure to the user.
    """
    jd = job_dir or os.getcwd()
    full_msg = message
    if exc:
        full_msg = f"{message}: {exc}"
        log.critical(full_msg)
        log.debug(traceback.format_exc())
    else:
        log.critical(full_msg)

    # Write failure state so the frontend can surface it
    progress_path = os.path.join(jd, "progress.json")
    try:
        existing: dict = {}
        if os.path.exists(progress_path):
            with open(progress_path) as f:
                existing = json.load(f)
    except Exception:
        existing = {}

    existing.update({"status": "FAILED", "error": full_msg})
    if phase is not None:
        existing["phase"] = phase

    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as write_err:
        log.error(f"Could not write failure to progress.json: {write_err}")

    sys.exit(1)
