#!/usr/bin/env python3
import duckdb
import os
import sys
import json
import re
import datetime
import html as _html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log_utils import get_logger, fatal
from currency_utils import load_currency_info
from config_loader import load_report_settings, load_data_config

JOB_DIR = os.getcwd()
DB_PATH = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
log = get_logger("render_report")

# ── Report customization settings ─────────────────────────────────────────────
_RS = load_report_settings(JOB_DIR)

try:
    _PT_REASONS = load_data_config("passthrough_reasons.json").get("reasons", {})
except Exception:
    _PT_REASONS = {}


def rs_bool(key: str, default: bool = True) -> bool:
    """Parse a report setting as bool. Never raises."""
    v = str(_RS.get(key, "")).strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return default


def rs_num(key: str, default: float = 0.0) -> float:
    """Parse a report setting as number. Never raises."""
    try:
        return float(_RS.get(key, default))
    except (TypeError, ValueError):
        return default


# ── Column definitions for the detail table ───────────────────────────────────
# Each entry: (setting_key_or_None, header_label, cell_renderer_lambda)
# setting_key=None means the column is always on. The SAME list drives both
# the <th> header and every <td> body cell so they can never drift out of sync.
COLUMNS = [
    (None,              "#",               lambda r: str(r[20] if len(r) > 20 else "")),
    (None,              "AWS Service → GCP Service", lambda r: f'{r[4] or ""} → {r[2] or ""}'),
    (None,              "AWS Instance",    lambda r: str(r[13] or "—")),
    (None,              "GCP Mapping",     lambda r: str(r[14] or "—")),
    (None,              "Region",          lambda r: str(r[9] or r[10] or "—")),
    (None,              "AWS",             lambda r: fmt(r[5] or 0.0)),
    (None,              "GCP OD",          lambda r: fmt(r[6] or 0.0)),
    ("col_gcp_cud_1yr", "GCP 1yr CUD",     lambda r: fmt(r[7] or 0.0)),
    ("col_gcp_cud_3yr", "GCP 3yr CUD",     lambda r: fmt(r[8] or 0.0)),
    ("col_diff",        "Diff",            lambda r: f'+{fmt((r[5] or 0.0) - (r[6] or 0.0))}' if (r[5] or 0.0) > (r[6] or 0.0) else f'−{fmt(abs((r[5] or 0.0) - (r[6] or 0.0)))}' if abs((r[5] or 0.0) - (r[6] or 0.0)) > 0.005 else '$0.00'),
    ("col_pct_of_aws",  "GCP as % of AWS", lambda r: (f'{(r[6] or 0.0) / r[5] * 100:.0f}%' if (r[5] or 0.0) else "—")),
    ("col_line_items",  "Line items",      lambda r: "1"),
    ("col_region",      "AWS Region",      lambda r: str(r[9] or "—")),
]

_ACTIVE_COLUMNS = [c for c in COLUMNS if c[0] is None or rs_bool(c[0])]
_NUM_COLS = len(_ACTIVE_COLUMNS) + 1  # +1 for the always-shown Details column

# Settings version hash: SHA-256 of sorted key=value pairs, first 12 hex chars.
import hashlib as _hashlib
_rs_version = _hashlib.sha256(
    ";".join(f"{k}={v}" for k, v in sorted(_RS.items())).encode()
).hexdigest()[:12]


# ── service-type pill classifier ──────────────────────────────────────────────
_PILL_RULES = [
    ("compute",    ["compute engine", "gce", "cloud run", "vertex ai", "cloud functions",
                    "batch", "app engine", "preemptible"]),
    ("database",   ["cloud sql", "cloud spanner", "alloydb", "bigtable", "firestore",
                    "datastore", "memorystore", "cloud memorystore", "database migration"]),
    ("storage",    ["cloud storage", "filestore", "backup", "persistent disk",
                    "hyperdisk", "storage transfer"]),
    ("network",    ["cloud nat", "network connectivity", "ncc", "load balanc",
                    "cloud cdn", "cloud dns", "cloud armor", "vpc", "interconnect",
                    "network services", "cloud vpn", "traffic director"]),
    ("monitoring", ["cloud logging", "cloud monitoring", "cloud trace", "cloud profiler",
                    "cloud audit", "error reporting", "cloud debugger"]),
    ("container",  ["kubernetes", "gke", "artifact registry", "container registry"]),
    ("messaging",  ["pub/sub", "pubsub", "cloud tasks", "dataflow", "cloud composer",
                    "eventarc", "workflows"]),
]

def pill_type(gcp_service, product=""):
    svc = (gcp_service or "").lower()
    prod = (product or "").lower()
    for ptype, keywords in _PILL_RULES:
        if any(k in svc for k in keywords):
            return ptype
        if any(k in prod for k in keywords):
            return ptype
    return "other"

_PILL_COLORS = {
    "compute":    "#1A73E8",
    "storage":    "#0D9D58",
    "database":   "#7B2FBE",
    "network":    "#E37400",
    "messaging":  "#C5221F",
    "monitoring": "#137333",
    "container":  "#1558D6",
    "other":      "#80868B",
}

_PILL_LABELS = {
    "compute":    "Compute",
    "storage":    "Storage",
    "database":   "Database",
    "network":    "Network",
    "messaging":  "Messaging",
    "monitoring": "Monitoring",
    "container":  "Container",
    "other":      "Other",
}


def pill_html(ptype):
    color = _PILL_COLORS.get(ptype, "#80868B")
    label = _PILL_LABELS.get(ptype, "Other")
    return (f'<span style="display:inline-block;font-size:10px;font-weight:600;'
            f'color:#fff;background:{color};border-radius:3px;padding:1px 5px;'
            f'margin-right:5px;vertical-align:middle;letter-spacing:0.3px">'
            f'{label}</span>')


def pct_badge(aws, gcp):
    if not aws or aws == 0:
        return ""
    pct = (gcp - aws) / aws * 100
    if abs(pct) < 0.5:
        return '<span style="color:#5F6368;font-size:11px">(≈flat)</span>'
    color = "#0D9D58" if pct < 0 else "#D93025"
    sign  = "−" if pct < 0 else "+"
    return (f'<span style="color:{color};font-size:11px;font-weight:600">'
            f'({sign}{abs(pct):.1f}%)</span>')


_GLOBAL_CURRENCY_SYMBOL = "$"


def fmt(v, prefix=None):
    if v is None:
        return "—"
    p = prefix if prefix is not None else _GLOBAL_CURRENCY_SYMBOL
    return f"{p}{v:,.2f}"



_AWS_LABEL_STRIP_RE = re.compile(
    r"^(Amazon|AWS)\s+|Global-|APS\d-|USE\d-|EU\w*-", re.IGNORECASE
)


def _short_aws_label(product):
    """Short human-readable AWS component label for the 'AWS Instance' column
    on non-instance rows (NAT Gateway, WAF, Route 53, etc.) — this column was
    previously blank whenever `instance_type` was NULL, i.e. for every row
    that isn't a literal EC2/RDS instance. Strips the generic 'Amazon'/'AWS'
    product prefix and common region/version code noise so a raw product
    string like 'Amazon Elastic Compute Cloud NatGateway' reads as 'Elastic
    Compute Cloud NatGateway' — not exact-match to any curated label set, but
    always something readable instead of a bare '—'."""
    if not product:
        return None
    label = _AWS_LABEL_STRIP_RE.sub("", str(product)).strip(" -")
    return label or str(product)


# AWS instance+license split: shared single source of truth (used by export_report too).
from license_split import load_ec2_specs as _load_ec2_specs, get_aws_license_split as _get_aws_license_split



# Signal phrases that only ever show up in internal engineering commentary
# (references to our own function/file names, "mode" bookkeeping, or bug-fix
# narration) — never in a customer-facing pricing rationale. Used to strip
# such commentary out of projection_note text before it reaches the report,
# regardless of what data source it came from (service_map.json "reason"
# fields, static-mapper notes, LLM output, etc.) or how it's punctuated.
_DEV_NOTE_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"\w+\.py\b"                                   # a source filename, e.g. apply_static_mappings.py
    r"|\bmap_\w+\(\)"                               # a function call, e.g. map_efs()
    r"|mode\s*=\s*['\"]?(?:keep|review)\b"          # internal mode bookkeeping
    r"|mode changed \w+->\w+"
    r"|unconditionally overwrit\w*"
    r"|clobber\w*"
    r"|same bug class\b"
    r"|confirmed (?:real|empirically)\b"
    r"|silently (?:force-mapped|overwritten|switched)\b"
    r")",
    re.IGNORECASE,
)


def _strip_dev_notes(text):
    """Drop any clause of `text` that reads as internal engineering commentary
    rather than customer-facing rationale (see _DEV_NOTE_SIGNAL_RE). Splits on
    sentence/clause boundaries (". ", " — ", " - ") and removes every clause
    from the first flagged one onward, since a debug aside typically explains
    itself in everything that follows it too."""
    if not text or not _DEV_NOTE_SIGNAL_RE.search(text):
        return text
    clauses = re.split(r"(\.\s+|\s+[—-]\s+)", text)
    kept = []
    for i in range(0, len(clauses), 2):
        clause = clauses[i]
        if _DEV_NOTE_SIGNAL_RE.search(clause):
            break
        kept.append(clause)
        if i + 1 < len(clauses):
            kept.append(clauses[i + 1])
    cleaned = "".join(kept).rstrip()
    cleaned = re.sub(r"[\s;—-]+$", "", cleaned).strip()
    if cleaned and not cleaned.endswith((".", ")", "]")):
        cleaned += "."
    return cleaned


def _format_why_html(notes_raw):
    """Parse a combined projection_note string into structured HTML.

    Returns (why_html, cost_tier_html).

    The note string is a · -separated concatenation of per-component notes
    (one per billing component — core, RAM, OS license, GPU, etc.).  We parse
    it into labelled rows so the customer sees a clean breakdown instead of a
    wall of internal mapping text, and collapse any detail/caveat text behind
    a <details> toggle so it never wraps the table cell to unreadable lengths.
    """
    e = _html.escape

    raw = (notes_raw or "").replace(" | ", " · ")

    # ── strip internal bookkeeping tokens ─────────────────────────────────────
    raw = re.sub(r"\s*\[validator:[^\]]*\]", "", raw)
    raw = re.sub(r"\s*\(rule=service_map_v1[^)]*\)", "", raw)
    raw = raw.strip(" ;")
    raw = _strip_dev_notes(raw)

    # ── extract cost-tier aside ────────────────────────────────────────────────
    cost_tier_html = ""
    m = re.search(r"\[cost-tier:\s*([^\]]+)\]", raw)
    if m:
        cost_tier_html = f'<div class="cost-tier-note">{e(m.group(1).strip())}</div>'
        raw = (raw[:m.start()] + raw[m.end():]).strip(" ;")

    if not raw:
        return "", cost_tier_html

    # ── split into per-component segments ─────────────────────────────────────
    segments = [s.strip() for s in raw.split(" · ") if s.strip()]

    # ── classify each segment ─────────────────────────────────────────────────
    # Structured buckets used to build the labelled-row display.
    gcp_targets   = []   # primary "GCE Family X" lines
    os_licenses   = []   # RHEL / Windows license lines
    inline_notes  = []   # [Note: …] extracted from any segment
    arch_caveats  = []   # [architecture review recommended: …]
    verify_flags  = []   # [verify with customer …] / "verify with customer"
    other_lines   = []   # free-form lines that don't fit above

    _DET_MAP  = "Deterministic mapping:"
    _DET_OS   = "Deterministic OS-license mapping:"
    _DET_DB   = "Deterministic DB mapping:"
    _GPU_MAP  = "GPU workload mapping:"
    _GPU_ACCEL = "Nvidia Accelerator attachment"
    _NOTE_RE  = re.compile(r'\[Note:\s*([^\]]+)\]', re.IGNORECASE)
    _ARCH_RE  = re.compile(r'\[architecture review recommended:\s*([^\]]+)\]', re.IGNORECASE)
    _VFY_RE   = re.compile(r'\[verify with customer[^\]]*\]', re.IGNORECASE)
    _VFY2_RE  = re.compile(r'verify with customer', re.IGNORECASE)
    # burstable / family-switch free-form: "t3a.medium → N2D AMD (2 vCPU / 4.0 GiB)"
    _ARROW_RE = re.compile(
        r'^([\w.\-]+)\s*[→\->]+\s*([\w\s.]+?)\s*\((\d+)\s*vCPU\s*/\s*([\d.]+)\s*GiB?\)',
        re.IGNORECASE,
    )
    # no-rate suffix: " [no rate in region …]" — strip, it's an ops detail
    _NO_RATE_RE = re.compile(r'\s*\[no rate in region[^\]]*\]', re.IGNORECASE)

    for seg in segments:
        seg = _NO_RATE_RE.sub("", seg)

        # Pull bracketed sub-tokens out before classifying the remainder.
        for m2 in _NOTE_RE.finditer(seg):
            inline_notes.append(m2.group(1).strip())
        for m2 in _ARCH_RE.finditer(seg):
            arch_caveats.append(m2.group(1).strip())
        for m2 in _VFY_RE.finditer(seg):
            txt = m2.group(0)
            inner = re.sub(r'^\[verify with customer[:\s]*', '', txt, flags=re.IGNORECASE).rstrip(']').strip()
            verify_flags.append(inner or "verify workload performance with customer")

        clean = _NOTE_RE.sub("", seg)
        clean = _ARCH_RE.sub("", clean)
        clean = _VFY_RE.sub("", clean)
        clean = clean.strip(" ;·")

        if not clean:
            continue

        if clean.startswith(_DET_OS):
            os_licenses.append(clean[len(_DET_OS):].strip())

        elif clean.startswith(_DET_DB):
            # "Deterministic DB mapping: Cloud SQL for MySQL standard vCPU (N2D family)"
            part = clean[len(_DET_DB):].strip()
            gcp_targets.append(part)

        elif clean.startswith(_DET_MAP):
            part = clean[len(_DET_MAP):].strip()
            part = re.sub(r'^GCE\s+', '', part)
            gcp_targets.append(part)

        elif clean.startswith(_GPU_MAP):
            # "GPU workload mapping: GCE G2 Core" → "G2 (paired compute)"
            part = clean[len(_GPU_MAP):].strip()
            part = re.sub(r'^GCE\s+', '', part)
            gcp_targets.append(part)

        elif clean.lower().startswith(_GPU_ACCEL.lower()):
            # "Nvidia Accelerator attachment (Nvidia L4 GPU)" → separate GPU row
            accel = re.search(r'\(([^)]+)\)', clean)
            label = accel.group(1) if accel else clean
            # "[no exact GCP equivalent for A10G; substituted L4 — …]" →
            # split into an inline note (the substitution fact) + a verify flag
            alias = re.search(r'\[no exact GCP equivalent[^\]]*\]', clean, re.IGNORECASE)
            if alias:
                alias_body = alias.group(0).strip('[]')
                # keep up to first semicolon as the note; rest is verify caveat
                parts = alias_body.split(';', 1)
                # Preserve original capitalisation of GPU model names (A10G, L4, etc.)
                inline_notes.append(parts[0].strip())
                if len(parts) > 1:
                    tail = parts[1].strip().lstrip('- ')
                    verify_flags.append(tail[:1].upper() + tail[1:] if tail else "")
                clean = clean[:alias.start()].strip()
                accel2 = re.search(r'\(([^)]+)\)', clean)
                label = accel2.group(1) if accel2 else label
            gcp_targets.append(f"\x00gpu\x00{label}")  # sentinel for GPU accelerator row

        elif _ARROW_RE.match(clean):
            # burstable / family-switch: "t3a.medium → N2D AMD (2 vCPU / 4.0 GiB)"
            am = _ARROW_RE.match(clean)
            src, fam, vcpu, ram = am.group(1), am.group(2).strip(), am.group(3), am.group(4)
            gcp_targets.append(fam)
            inline_notes.append(f"Source: {src} · {vcpu} vCPU / {ram} GiB")

        elif re.match(r'^S3 .+\s*[→\->]+\s*GCS .+ Class [AB] Operations', clean, re.IGNORECASE):
            # "S3 Glacier Instant Retrieval GET → GCS Coldline Class B Operations ($0.05/10k; note)"
            # Extract GCS target and parenthetical rate/note so they render as structured rows.
            m3 = re.match(
                r'^S3 .+\s*[→\->]+\s*GCS\s+(.+?)\s*\(([^)]+)\)\s*(.*)$',
                clean, re.IGNORECASE,
            )
            if m3:
                gcp_targets.append(m3.group(1).strip())
                rate_note = m3.group(2).strip()
                # split on semicolon: first part = rate claim, second = pricing caveat
                halves = rate_note.split(";", 1)
                inline_notes.append(halves[0].strip())
                if len(halves) > 1:
                    caveat = halves[1].strip()
                    # "GCS is 2× cheaper …" is a real pricing delta — surface it as a verify flag
                    verify_flags.append(caveat[:1].upper() + caveat[1:] if caveat else "")
                if m3.group(3).strip():
                    inline_notes.append(m3.group(3).strip())
            else:
                other_lines.append(clean)

        elif clean.startswith("Local SSD"):
            gcp_targets.append("Local SSD (storage-optimized attachment)")

        elif clean.startswith("[Networking]") or clean.startswith("["):
            # service_map.json reason note — strip the category tag
            body = re.sub(r'^\[[^\]]+\]\s*', '', clean)
            # strip ". NOTE: mode changed …" bookkeeping tail
            body = re.sub(r'\.\s*NOTE:\s*mode changed.*$', '', body, flags=re.IGNORECASE).strip()
            other_lines.append(body)

        else:
            # Plain free-form note — check for verify-with-customer inline
            if _VFY2_RE.search(clean):
                # split at the verify phrase so the main line stays clean
                parts = _VFY2_RE.split(clean, maxsplit=1)
                if parts[0].strip():
                    other_lines.append(parts[0].strip(" ;—"))
                # the phrase itself was already harvested above if in brackets;
                # if it's bare text, add a generic flag
                if not any("verify" in f.lower() for f in verify_flags):
                    verify_flags.append("verify workload performance with customer")
            else:
                other_lines.append(clean)

    # ── deduplicate while preserving order ────────────────────────────────────
    def _dedup(lst):
        seen, out = set(), []
        for x in lst:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    gcp_targets  = _dedup(gcp_targets)
    os_licenses  = _dedup(os_licenses)
    inline_notes = _dedup(inline_notes)
    arch_caveats = _dedup(arch_caveats)
    verify_flags = _dedup(verify_flags)
    other_lines  = _dedup(other_lines)

    # ── collapse redundant component pairs into one label ────────────────────
    # "N2D AMD Core" + "N2D AMD RAM"  →  "N2D AMD"
    # "Cloud SQL for MySQL standard vCPU" + "…standard RAM"  →  "Cloud SQL for MySQL standard"
    # Normalise a target string to its "family" by stripping trailing component
    # suffixes, including parenthetical qualifiers like "(N2D AMD family)".
    # "N2D AMD Core"                               → "N2D AMD"
    # "Cloud SQL for MySQL standard vCPU (N2D ...)" → "Cloud SQL for MySQL standard"
    _COMP_PAT = re.compile(
        r'\s*(Core|RAM|vCPU|ram|core)\s*(?:\([^)]*\))?\s*$', re.IGNORECASE)
    def _strip_comp(t):
        return _COMP_PAT.sub('', t).strip()

    plain_targets = [t for t in gcp_targets if not t.startswith('\x00gpu\x00')]
    gpu_targets   = [t[5:] for t in gcp_targets if t.startswith('\x00gpu\x00')]

    _IS_CORE = re.compile(r'\s+(Core|vCPU|core)\b', re.IGNORECASE)
    _IS_RAM  = re.compile(r'\s+(RAM|ram)\b', re.IGNORECASE)
    cores = {_strip_comp(t) for t in plain_targets if _IS_CORE.search(t)}
    rams  = {_strip_comp(t) for t in plain_targets if _IS_RAM.search(t)}
    families = cores & rams

    collapsed = []
    for fam in sorted(families):
        collapsed.append(fam)
    for t in plain_targets:
        base = _strip_comp(t)
        if base not in families:
            # Still strip the suffix even when unpaired — "T2A Arm Core" → "T2A Arm"
            collapsed.append(base if _COMP_PAT.search(t) else t)
    gcp_targets = _dedup(collapsed) + [f"\x00gpu\x00{g}" for g in _dedup(gpu_targets)]

    # ── pretty-print helpers ───────────────────────────────────────────────────
    def row(key, val_html, cls="why-row-val"):
        return (f'<div class="why-row">'
                f'<span class="why-row-key">{e(key)}</span>'
                f'<span class="{cls}">{val_html}</span>'
                f'</div>')

    def chip(label, cls):
        return f'<span class="why-chip {cls}">{e(label)}</span>'

    # ── build primary (always-visible) rows ───────────────────────────────────
    primary_rows = []

    if gcp_targets:
        # Annotate family names with a short descriptor chip when recognisable.
        _FAMILY_CHIPS = {
            "C2D AMD": ("Compute-opt · AMD EPYC", "chip-new-gen"),
            "C3D":     ("Compute-opt · AMD Genoa", "chip-new-gen"),
            "C3":      ("Compute-opt · Intel", "chip-new-gen"),
            "C4":      ("Compute-opt · Intel", "chip-new-gen"),
            "C4D":     ("Compute-opt · AMD", "chip-new-gen"),
            "C4A Arm": ("Compute-opt · ARM", "chip-new-gen"),
            "N2D AMD": ("General · AMD EPYC", "chip-new-gen"),
            "N4":      ("General · Intel", "chip-new-gen"),
            "N4D":     ("General · AMD Genoa", "chip-new-gen"),
            "T2A Arm": ("General · ARM", "chip-new-gen"),
            "G2":      ("Paired compute · L4", "chip-arch"),
            "A2":      ("Paired compute · A100", "chip-arch"),
            "A3":      ("Paired compute · H100", "chip-arch"),
            # GCS operation SKUs — matched on prefix so "Coldline Class B Operations" hits first
            "Archive Class B Operations":  ("Cold storage · 365-day min", "chip-arch"),
            "Coldline Class B Operations": ("Cold storage · 90-day min",  "chip-arch"),
            "Standard Class B Operations": ("Object storage reads",        "chip-new-gen"),
            "Standard Class A Operations": ("Object storage writes",       "chip-new-gen"),
        }
        has_gpu_row = any(t.startswith('\x00gpu\x00') for t in gcp_targets)
        for fam in gcp_targets:
            if fam.startswith('\x00gpu\x00'):
                gpu_label = fam[5:]
                primary_rows.append(row("GPU accelerator", e(gpu_label)))
                continue
            label = fam
            ch = ""
            # Only show machine-family chip when there is no separate GPU row
            # (to avoid redundant "GPU · L4" on the G2 row + the GPU row).
            if not has_gpu_row:
                for key, (desc, cls_) in _FAMILY_CHIPS.items():
                    if label.startswith(key):
                        ch = chip(desc, cls_)
                        break
            else:
                # With a GPU row, just add a plain "Compute" chip for the paired family
                for key in _FAMILY_CHIPS:
                    if label.startswith(key):
                        ch = chip("Paired compute", "chip-new-gen")
                        break
            primary_rows.append(row("GCP target", e(label) + ch))

    if os_licenses:
        for lic in os_licenses:
            # Make RHEL band human-readable: "Red Hat Enterprise Linux 9 on VM with up to 8 VCPU"
            # → "RHEL 9 · up to 8 vCPU tier"
            display = lic
            rhel_m = re.match(
                r'Red Hat Enterprise Linux (\d+) on VM with (.+)', lic, re.IGNORECASE)
            if rhel_m:
                band = rhel_m.group(2).replace("VCPU", "vCPU").replace("vcpu", "vCPU")
                display = f"RHEL {rhel_m.group(1)} — {band}"
            win_m = re.match(r'Windows Server.*?(\d{4}).*?(Core|BYOL|per vCPU.*)?$', lic, re.IGNORECASE)
            if win_m:
                display = f"Windows Server {win_m.group(1)} license"
                if win_m.group(2):
                    display += f" ({win_m.group(2)})"
            primary_rows.append(row("OS license", e(display)))

    for line in other_lines:
        primary_rows.append(row("Note", e(line)))

    # ── build detail (collapsible) rows ───────────────────────────────────────
    detail_rows = []

    if inline_notes:
        for note in inline_notes:
            detail_rows.append(row("Detail", e(note)))

    if arch_caveats:
        for cav in arch_caveats:
            detail_rows.append(row("⚠ Architecture", e(cav), cls="why-row-caveat"))

    if verify_flags:
        for flag in verify_flags:
            detail_rows.append(row("☑ Verify", e(flag), cls="why-row-caveat"))

    # ── assemble HTML ─────────────────────────────────────────────────────────
    if not primary_rows and not detail_rows:
        # Fallback: render as plain text (unusual / passthrough rows).
        fallback = e(raw[:320] + ("…" if len(raw) > 320 else ""))
        return (
            f'<div class="why-block">'
            f'<span class="why-label">Why this equivalent:</span>'
            f'<span class="why-text">{fallback}</span>'
            f'</div>',
            cost_tier_html,
        )

    rows_html = '<div class="why-rows">' + "".join(primary_rows) + '</div>'

    if detail_rows:
        detail_html = '<div class="why-detail-rows">' + "".join(detail_rows) + '</div>'
        expand_html = (
            '<details class="why-expand">'
            '<summary>'
            '<span class="why-toggle">'
            '<span class="why-toggle-arrow">▶</span> details</span>'
            '</summary>'
            + detail_html +
            '</details>'
        )
    else:
        expand_html = ""

    html = (
        f'<div class="why-block">'
        f'<span class="why-label">Why this equivalent:</span>'
        + rows_html
        + expand_html
        + f'</div>'
    )
    return html, cost_tier_html


CSS = """
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font-family: 'Google Sans', 'Roboto', Arial, sans-serif;
    margin: 0; padding: 0;
    color: #202124; background: #fff;
    font-size: 13px; line-height: 1.5;
  }
  .page-body { padding: 28px 36px; }
  h1 {
    color: #1A73E8; font-size: 26px; font-weight: 500;
    border-bottom: 3px solid #1A73E8;
    padding-bottom: 10px; margin: 0 0 6px;
  }
  .subhead { color: #5F6368; margin: 0 0 28px; font-size: 13px; }
  .subhead b { color: #202124; }
  h2 {
    color: #1A73E8; font-size: 16px; font-weight: 500;
    margin: 32px 0 10px; padding-bottom: 4px;
    border-bottom: 1px solid #e8eaed;
  }
  h3 { font-size: 13px; font-weight: 600; margin: 16px 0 6px; color: #202124; }
  .table-scroll { overflow-x: auto; }
  .extra-col { display: none; }
  table.show-extra .extra-col { display: table-cell; }
  .extra-toggle {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; color: #5F6368; cursor: pointer; margin: 4px 0 10px;
  }
  .extra-toggle input { cursor: pointer; }
  table { border-collapse: collapse; width: 100%; }
  th {
    background: #1A73E8; color: #fff;
    padding: 8px 12px; text-align: left;
    font-weight: 500; font-size: 12px; white-space: nowrap;
  }
  td { border-bottom: 1px solid #e8eaed; padding: 7px 12px; vertical-align: top; }
  tr:hover td { background: #f8f9fa; }
  .total-row td {
    background: #f1f3f4 !important; font-weight: 600;
    border-top: 2px solid #1A73E8; border-bottom: none;
  }
  .subtotal-row td {
    background: #f8f9fa !important; font-weight: 600;
    border-top: 1px solid #1A73E8; border-bottom: 2px solid #e8eaed;
  }
  .section-header-row th {
    background: #e8f0fe !important; color: #1967d2 !important;
    font-size: 13px; font-weight: 600; padding: 10px 12px;
    border-top: 2px solid #1A73E8; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .green { color: #0D9D58; }
  .red   { color: #D93025; }
  .row-passthrough { background: #f8f9fa; }
  .row-passthrough td { color: #9aa0a6 !important; }
  .row-passthrough .num.green, .row-passthrough .num.red { color: #9aa0a6 !important; }
  .row-passthrough a { color: #9aa0a6 !important; }
  .row-free { background: #e8f0fe; }
  .row-free td { color: #1967D2 !important; }
  .row-free .num.green, .row-free .num.red { color: #1967D2 !important; }
  .row-free a { color: #1967D2 !important; }
  .free-tier { font-style: italic; }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 28px;
  }
  .card {
    border: 1px solid #e8eaed; border-radius: 8px;
    padding: 14px 16px; background: #fff;
  }
  .card-label { font-size: 11px; color: #5F6368; text-transform: uppercase;
                letter-spacing: 0.5px; margin-bottom: 4px; }
  .card-value { font-size: 20px; font-weight: 600; color: #202124; }
  .card-sub   { font-size: 11px; color: #5F6368; margin-top: 2px; }
  .card-accent { border-top: 3px solid #1A73E8; }
  .info-box {
    background: #f8f9fa; border-left: 3px solid #1A73E8;
    padding: 10px 16px; margin: 10px 0; font-size: 12px;
  }
  .warn-box {
    background: #fff8e1; border-left: 3px solid #F9A825;
    padding: 10px 16px; margin: 10px 0; font-size: 12px;
  }
  .info-box ul, .warn-box ul { margin: 4px 0; padding-left: 18px; }
  .info-box li, .warn-box li { margin-bottom: 3px; }
  .desc { font-size: 11px; color: #5F6368; line-height: 1.45; max-width: 380px; }
  .why-block { margin-top: 4px; padding-left: 8px; border-left: 2px solid #E8EAED; }
  .why-label { font-size: 10px; font-weight: 600; color: #1A73E8; letter-spacing: 0.2px; display: block; margin-bottom: 3px; }
  .why-text  { font-size: 11px; color: #5F6368; line-height: 1.45; }
  .why-rows  { display: flex; flex-direction: column; gap: 3px; margin-top: 2px; }
  .why-row   { display: flex; gap: 6px; align-items: baseline; font-size: 11px; line-height: 1.4; }
  .why-row-key   { font-size: 10px; font-weight: 600; color: #80868B; white-space: nowrap; min-width: 80px; text-transform: uppercase; letter-spacing: 0.3px; }
  .why-row-val   { color: #202124; }
  .why-row-caveat { color: #B06000; }
  .why-row-warn   { color: #C5221F; }
  .why-chip { display: inline-block; font-size: 9px; font-weight: 600; padding: 1px 6px; border-radius: 100px; margin-left: 4px; vertical-align: middle; }
  .chip-arch  { background: #FFF3E0; color: #E65100; }
  .chip-verify { background: #FFF8E1; color: #B06000; }
  .chip-new-gen { background: #E8F5E9; color: #1B5E20; }
  .why-expand { margin-top: 3px; }
  .why-expand summary { cursor: pointer; list-style: none; display: inline-flex; align-items: center; gap: 4px; }
  .why-expand summary::-webkit-details-marker { display: none; }
  .why-toggle { font-size: 10px; color: #1A73E8; font-weight: 500; user-select: none; }
  .why-toggle-arrow { font-size: 9px; display: inline-block; transition: transform 0.15s; }
  .why-expand[open] .why-toggle-arrow { transform: rotate(90deg); }
  .why-detail-rows { margin-top: 4px; padding-top: 4px; border-top: 1px dashed #E8EAED; display: flex; flex-direction: column; gap: 3px; }
  .cost-tier-note { font-size: 9px; font-weight: 300; color: #9AA0A6; font-style: italic; margin-top: 2px; }
  .row-license-sub td { background: #fafafa; font-size: 11px; padding: 4px 12px; border-top: none; }
  .row-license-sub:hover td { background: #f1f3f4; }
  .plus-sign { font-weight: 600; color: #1A73E8; margin-right: 3px; }
  .strat-pt { font-size: 10px; color: #80868B; margin-left: 4px; }
  .cud-na-marker { font-size: 10px; color: #EA8600; cursor: help; margin-left: 1px; }
  .legend { font-style: italic; color: #5F6368; font-size: 12px; margin-top: 8px; }
  .section-meta { font-size: 11px; color: #80868B; margin: -6px 0 10px; }
  ul.method { margin: 6px 0; padding-left: 20px; font-size: 13px; line-height: 1.8; }
  .fc-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 36px; border-bottom: 1px solid #e8eaed; background: #fff;
  }
  .fc-logo-link { display: flex; align-items: center; gap: 10px; text-decoration: none; }
  .fc-tagline { font-size: 10px; color: #80868B; margin-top: 2px; letter-spacing: 0.4px; }
  .fc-right { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
  .fc-badge {
    font-size: 10px; color: #1A73E8; background: #E8F0FE;
    border-radius: 100px; padding: 3px 12px; font-weight: 500; white-space: nowrap;
  }
  .fc-prepared { font-size: 10px; color: #80868B; }
  .fc-prepared b { color: #5F6368; font-weight: 600; }
  .fc-divider {
    height: 2px;
    background: linear-gradient(90deg, #645DF6 0%, #00C2BB 60%, transparent 100%);
  }
  .fc-footer {
    margin-top: 48px; padding-top: 18px; border-top: 1px solid #e8eaed;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 8px;
  }
  .fc-footer-brand {
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; color: #80868B; text-decoration: none;
  }
  .fc-footer-brand b { color: #5F6368; }
  .fc-footer-note { font-size: 10px; color: #BDC1C6; }
</style>
"""


def main():
    if not os.path.exists(DB_PATH):
        fatal(log, "projection.duckdb not found — cannot generate report", phase=6)
        return

    conn = duckdb.connect(DB_PATH)

    # ── multi-currency resolution (H-6) ───────────────────────────────────────
    currency_info = load_currency_info(JOB_DIR, conn)
    curr_code = currency_info.get("currency", "USD")
    curr_symbol = currency_info.get("symbol", "$")
    fx_rate = float(currency_info.get("fx_rate", 1.0))
    aws_fx_rate = float(currency_info.get("aws_fx_rate", 1.0))

    global _GLOBAL_CURRENCY_SYMBOL
    _GLOBAL_CURRENCY_SYMBOL = curr_symbol

    # ── coverage KPI ──────────────────────────────────────────────────────────
    try:
        coverage_rows = conn.execute("""
            SELECT c.mechanic_group,
                SUM(CASE WHEN COALESCE(m.strategy, '') != 'passthrough' THEN c.aws_amortized_cost ELSE 0 END) AS mapped_spend,
                SUM(c.aws_amortized_cost) AS total_spend
            FROM aws_li_catalog c
            LEFT JOIN aws_li_to_gcp_li m USING (aws_li_key)
            GROUP BY c.mechanic_group
        """).fetchall()
        coverage = {}
        for group, mapped, total in coverage_rows:
            if not group:
                continue
            pct = (mapped / total * 100.0) if total else 100.0
            coverage[group] = round(pct, 1)
        with open(os.path.join(JOB_DIR, "projection-audit", "mapping_coverage.json"), "w") as f:
            json.dump(coverage, f, indent=2)
    except Exception as e:
        log.warning(f"Coverage report failed: {e}")

    # ── totals ────────────────────────────────────────────────────────────────
    # aws_grand MUST equal the real invoice total — verified directly against a
    # real PDF bill: SUM(aws_amortized_cost) over every ingested row, no filter
    # at all, reproduced the bill's own stated Grand Total to the cent.
    #
    # commitment_discount rows (RIFee/SavingsPlanRecurringFee/EdpDiscount) used
    # to be excluded here on the theory that their cost is "already reflected
    # in effective rates" of the DiscountedUsage rows they fund, i.e. summing
    # both double-counts the same dollars. That reasoning assumes AWS's own
    # amortized-cost redistribution already folded the fee into the usage
    # rows' rates. Confirmed real bug: it does NOT hold for PDF-ingested bills
    # — ingest.py explicitly SKIPS the paired "<instance> usage covered by
    # Compute Savings Plans (USD Y)" negative-offset lines (see ingest.py's
    # "Skip Savings-Plan/RI 'covered by' lines" comment), so the usage rows
    # here carry the full GROSS on-demand rate, not a discount-inclusive rate
    # — nothing else in the ingested data accounts for the Savings Plan's real
    # dollar cost. Excluding the fee row on top of that silently dropped real
    # spend from the headline total (confirmed live: a bill's true $15,721.87
    # total under-reported as $10,663.15 — off by exactly the $5,058.72
    # Savings Plan recurring fee). Trusting the verified ground truth (sum of
    # every row) instead of a format-dependent heuristic.
    aws_workload     = (conn.execute("SELECT COALESCE(SUM(aws_amortized_cost),0) FROM aws_li_catalog WHERE is_workload").fetchone()[0]) * aws_fx_rate
    aws_non_workload = (conn.execute("SELECT COALESCE(SUM(aws_amortized_cost),0) FROM aws_li_catalog WHERE NOT is_workload").fetchone()[0]) * aws_fx_rate
    aws_grand = aws_workload + aws_non_workload

    # Gross bill before any credit/discount/EDP-reconciliation row nets
    # against it — i.e. every POSITIVE charge, ignoring the negative rows
    # (EDP reconciliation, refunds, "covered by Savings Plans" nettings,
    # etc.) that bring the total down to what you actually pay. aws_grand
    # above already nets these in (it's the real invoice total); this is
    # just the other half of that same number, shown alongside it so a
    # reader can see both "what you're billed" and "what was discounted off"
    # without doing the subtraction themselves.
    aws_gross = (conn.execute(
        "SELECT COALESCE(SUM(aws_amortized_cost),0) FROM aws_li_catalog WHERE aws_amortized_cost > 0"
    ).fetchone()[0]) * aws_fx_rate
    aws_discounts = aws_gross - aws_grand

    # Marketplace spend: non-workload rows that are passthrough with "Marketplace" in note
    aws_marketplace = (conn.execute("""
        SELECT COALESCE(SUM(c.aws_amortized_cost), 0)
        FROM aws_li_catalog c
        JOIN aws_li_to_gcp_li m USING (aws_li_key)
        WHERE NOT c.is_workload
          AND (m.projection_note ILIKE '%marketplace%' OR c.product ILIKE '%marketplace%')
    """).fetchone()[0]) * aws_fx_rate
    # Infrastructure baseline = everything except marketplace (the "true" IaaS+PaaS spend)
    aws_infra_baseline = aws_grand - aws_marketplace

    gcp_od   = (conn.execute("SELECT COALESCE(SUM(gcp_projected_cost),0) FROM gcp_projection WHERE is_workload").fetchone()[0]) * fx_rate
    # For the summary totals, rows without a CUD rate fall back to their OD cost so the
    # grand total reflects actual expected spend; per-row display uses NULL to show "—".
    gcp_1yr  = (conn.execute("SELECT COALESCE(SUM(COALESCE(gcp_cost_1yr_cud, gcp_projected_cost)),0) FROM gcp_projection WHERE is_workload").fetchone()[0]) * fx_rate
    gcp_3yr  = (conn.execute("SELECT COALESCE(SUM(COALESCE(gcp_cost_3yr_cud, gcp_projected_cost)),0) FROM gcp_projection WHERE is_workload").fetchone()[0]) * fx_rate

    # Mapped AWS cost = workload rows that received a real GCP projected cost
    # (strategy map or break_down). Passthrough rows carry AWS cost 1:1 and
    # inflate the baseline if included — comparing GCP vs mapped-only is apples-to-apples.
    # EXISTS subquery ensures each aws_li_key is counted once even for break_down rows
    # that join multiple components. SUM(DISTINCT ...) is wrong here — it deduplicates
    # by value, not by key, which mis-sums rows with coincident costs.
    aws_mapped_cost = (conn.execute("""
        SELECT COALESCE(SUM(c.aws_amortized_cost), 0)
        FROM aws_li_catalog c
        WHERE c.is_workload
          AND EXISTS (
            SELECT 1 FROM aws_li_to_gcp_li m
            WHERE m.aws_li_key = c.aws_li_key
              AND m.strategy IN ('map', 'break_down')
          )
    """).fetchone()[0]) * aws_fx_rate
    # Not shown in the report — the customer-facing summary compares GCP
    # against one number (AWS Total (Bill)) to keep it a one-glance read.
    # This more precise "priced rows only" figure is still logged for anyone
    # auditing why the % badges don't perfectly reconcile against a manual
    # row-by-row sum.
    log.info(f"AWS mapped cost (priced rows only, excl. passthrough/credits): {fmt(aws_mapped_cost)}")

    gcp_nw_od  = (conn.execute("SELECT COALESCE(SUM(gcp_projected_cost),0) FROM gcp_projection WHERE NOT is_workload").fetchone()[0]) * fx_rate
    gcp_nw_1yr = (conn.execute("SELECT COALESCE(SUM(COALESCE(gcp_cost_1yr_cud, gcp_projected_cost)),0) FROM gcp_projection WHERE NOT is_workload").fetchone()[0]) * fx_rate
    gcp_nw_3yr = (conn.execute("SELECT COALESCE(SUM(COALESCE(gcp_cost_3yr_cud, gcp_projected_cost)),0) FROM gcp_projection WHERE NOT is_workload").fetchone()[0]) * fx_rate

    gcp_grand_od  = gcp_od  + gcp_nw_od
    gcp_grand_1yr = gcp_1yr + gcp_nw_1yr
    gcp_grand_3yr = gcp_3yr + gcp_nw_3yr

    # ── metadata ──────────────────────────────────────────────────────────────
    customer_name = "Prospect"
    cust_file = os.path.join(JOB_DIR, "customer_name.txt")
    if os.path.exists(cust_file):
        with open(cust_file) as f:
            customer_name = f.read().strip() or "Prospect"

    try:
        utc = datetime.UTC
    except AttributeError:
        utc = datetime.timezone.utc
    now    = datetime.datetime.now(utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")

    region_row = conn.execute("""
        SELECT gcp_region, COUNT(*) n FROM gcp_projection
        WHERE is_workload AND gcp_region IS NOT NULL
        GROUP BY gcp_region ORDER BY n DESC LIMIT 1
    """).fetchone()
    gcp_region_display = region_row[0] if region_row else "see individual rows"
    _REGION_NAMES = {
        "asia-southeast1": "Singapore",  "asia-northeast1": "Tokyo",
        "asia-south1": "Mumbai",          "us-east4": "N. Virginia",
        "us-central1": "Iowa",            "us-west1": "Oregon",
        "us-west2": "Los Angeles",        "europe-west1": "Belgium",
        "europe-west4": "Netherlands",
    }
    if gcp_region_display in _REGION_NAMES:
        gcp_region_display = f"{gcp_region_display} ({_REGION_NAMES[gcp_region_display]})"

    total_li = conn.execute("SELECT COUNT(*) FROM aws_li_catalog").fetchone()[0]

    # ── capacity ──────────────────────────────────────────────────────────────
    aws_vcpu = conn.execute(
        "SELECT COALESCE(SUM(instance_vcpus*instance_count),0) FROM aws_li_catalog WHERE is_workload AND instance_vcpus IS NOT NULL"
    ).fetchone()[0]
    aws_ram  = conn.execute(
        "SELECT COALESCE(SUM(instance_ram_gb*instance_count),0) FROM aws_li_catalog WHERE is_workload AND instance_ram_gb IS NOT NULL"
    ).fetchone()[0]
    gcp_vcpu = conn.execute("""
        WITH cm AS (
            SELECT c.aws_li_key, SUM(m.unit_multiplier*c.instance_count) AS v
            FROM aws_li_to_gcp_li m JOIN aws_li_catalog c USING (aws_li_key)
            WHERE m.component='core' AND m.strategy IN ('map','break_down') GROUP BY c.aws_li_key
        )
        SELECT COALESCE(SUM(COALESCE(cm.v, c.instance_vcpus*c.instance_count)),0)
        FROM aws_li_catalog c LEFT JOIN cm USING (aws_li_key)
        WHERE c.is_workload AND c.instance_vcpus IS NOT NULL
    """).fetchone()[0]
    gcp_ram = conn.execute("""
        WITH rm AS (
            SELECT c.aws_li_key, SUM(m.unit_multiplier*c.instance_count) AS r
            FROM aws_li_to_gcp_li m JOIN aws_li_catalog c USING (aws_li_key)
            WHERE m.component='ram' AND m.strategy IN ('map','break_down') GROUP BY c.aws_li_key
        )
        SELECT COALESCE(SUM(COALESCE(rm.r, c.instance_ram_gb*c.instance_count)),0)
        FROM aws_li_catalog c LEFT JOIN rm USING (aws_li_key)
        WHERE c.is_workload AND c.instance_ram_gb IS NOT NULL
    """).fetchone()[0]

    _CAP_TOL = 0.005
    vcpu_ok = gcp_vcpu >= aws_vcpu * (1 - _CAP_TOL)
    ram_ok  = gcp_ram  >= aws_ram  * (1 - _CAP_TOL)

    # ── confidence ────────────────────────────────────────────────────────────
    avg_conf = conn.execute(
        "SELECT COALESCE(AVG(LEAST(mapping_confidence,1.0)),0) FROM aws_li_to_gcp_li WHERE strategy!='passthrough'"
    ).fetchone()[0]

    cat_conf_rows = conn.execute("""
        SELECT
          CASE
            WHEN m.strategy = 'passthrough' THEN 'Passthrough'
            WHEN c.product ILIKE '%EC2%' OR c.product ILIKE '%Elastic Compute%' THEN 'Compute (EC2)'
            WHEN c.product ILIKE '%RDS%' OR c.product ILIKE '%Aurora%' OR c.product ILIKE '%Redshift%' THEN 'Database (RDS/Aurora)'
            WHEN c.product ILIKE '%OpenSearch%' OR c.product ILIKE '%MSK%' OR c.product ILIKE '%ElastiCache%' THEN 'Managed Services'
            WHEN c.product ILIKE '%S3%' OR c.product ILIKE '%EBS%' OR c.product ILIKE '%Glacier%'
              OR c.product ILIKE '%Storage%' OR c.product ILIKE '%Backup%' THEN 'Storage'
            WHEN c.product ILIKE '%Route 53%' OR c.product ILIKE '%CloudFront%'
              OR c.product ILIKE '%VPC%' OR c.product ILIKE '%Data Transfer%' THEN 'Networking'
            ELSE 'Other'
          END AS category,
          AVG(LEAST(m.mapping_confidence, 1.0)) AS avg_conf,
          COUNT(*) AS cnt
        FROM aws_li_to_gcp_li m JOIN aws_li_catalog c USING (aws_li_key)
        GROUP BY category ORDER BY avg_conf DESC NULLS LAST
    """).fetchall()

    # ── validation warnings ───────────────────────────────────────────────────
    _GATE_LABELS = {
        "passthrough_on_mappable_service": "Some services could not be automatically mapped to GCP. These rows carry the AWS cost as a placeholder — manual review recommended.",
        "phantom_zero": "One or more rows show a unit-pricing discrepancy. Affected costs may be overstated.",
        "under_projection_gcp_zero": "Some billable AWS rows project to $0 on GCP — the rate card may be missing a SKU.",
        "capacity_reconciliation": "Projected GCP compute capacity is below the AWS baseline for some instance groups.",
        "cud_coverage_missing": "Committed Use Discount rates are unavailable for some services — those rows show On-Demand pricing.",
        "storage_transfer_over_projection": "Storage or data-transfer costs may be conservatively estimated (up to 3× AWS).",
        "instance_family_mismatch": "Some instance-family mappings may not be optimal. Review flagged rows below.",
        "reconciliation": "Projected AWS total differs slightly from the uploaded bill total.",
        "passthrough_budget": "A meaningful share of the workload is carried at AWS cost (no GCP equivalent mapped). Manual sizing recommended.",
    }
    validation_notes = []
    val_path = os.path.join(JOB_DIR, "validation_report.json")
    if os.path.exists(val_path):
        try:
            with open(val_path) as vf:
                vr = json.load(vf)
            for gate, rows in (vr.get("violations") or {}).items():
                if rows:
                    validation_notes.append(_GATE_LABELS.get(gate, f"Validation note: {gate}"))
        except Exception as e:
            log.debug(f"Could not load validation_report.json: {e}")

    # ── detail rows (one per aws_li_key, components aggregated) ──────────────
    # gcp_machine_type may be absent on a DB merged before this column existed
    # (older job, not re-run since) — fall back to a literal NULL rather than
    # erroring, same guarded-column pattern used elsewhere in this pipeline
    # (classify_mechanics.py's billing_format, aws_normalizer.py's
    # canonical_service).
    _m2g_cols = {r[1].lower() for r in conn.execute("PRAGMA table_info('aws_li_to_gcp_li')").fetchall()}
    _machine_type_expr = (
        "ANY_VALUE(m.gcp_machine_type)" if "gcp_machine_type" in _m2g_cols else "NULL"
    )
    _has_assumption = "is_assumption" in _m2g_cols
    _assumption_expr = "BOOL_OR(COALESCE(m.is_assumption, FALSE))" if _has_assumption else "FALSE"

    _c_cols = {r[1].lower() for r in conn.execute("PRAGMA table_info('aws_li_catalog')").fetchall()}
    _p_cols = {r[1].lower() for r in conn.execute("PRAGMA table_info('gcp_projection')").fetchall()}
    _commitment_basis_expr = (
        "COALESCE(ANY_VALUE(p.commitment_basis), 'on_demand')" if "commitment_basis" in _p_cols
        else "'on_demand'"
    )
    _c_od_val = "c.aws_od_cost" if "aws_od_cost" in _c_cols else "c.aws_amortized_cost"
    _p_od_val = "ANY_VALUE(p.aws_od_cost)" if "aws_od_cost" in _p_cols else ("ANY_VALUE(p.aws_cost_od_est)" if "aws_cost_od_est" in _p_cols else _c_od_val)
    _aws_cost_od_est_expr = f"COALESCE({_p_od_val}, {_c_od_val}, c.aws_amortized_cost) * {aws_fx_rate}"

    _c_basis_val = "ANY_VALUE(c.od_basis)" if "od_basis" in _c_cols else "'amortized_fallback'"
    _p_basis_val = "ANY_VALUE(p.od_basis)" if "od_basis" in _p_cols else _c_basis_val
    _od_basis_expr = f"COALESCE({_p_basis_val}, {_c_basis_val}, 'amortized_fallback')"

    rows = conn.execute(f"""
        SELECT
            c.product,
            c.operation,
            COALESCE(ANY_VALUE(m.gcp_service), 'N/A')                        AS gcp_service,
            ANY_VALUE(m.strategy)                                              AS strategy,
            COALESCE(STRING_AGG(DISTINCT m.projection_note, ' | '), '')       AS notes,
            c.aws_amortized_cost * {aws_fx_rate}                               AS aws_amortized_cost,
            SUM(p.gcp_projected_cost) * {fx_rate}                             AS gcp_od,
            SUM(p.gcp_cost_1yr_cud) * {fx_rate}                              AS gcp_1yr,
            SUM(p.gcp_cost_3yr_cud) * {fx_rate}                              AS gcp_3yr,
            c.aws_region,
            c.gcp_region,
            c.is_workload,
            c.pricing_model,
            c.instance_type,
            ANY_VALUE(m.gcp_sku_name)                                          AS gcp_sku_name,
            {_machine_type_expr}                                               AS gcp_machine_type,
            {_assumption_expr}                                                 AS is_assumption,
            c.aws_li_key,
            SUM(CASE WHEN m.component NOT IN ('license', 'sql_license') THEN p.gcp_projected_cost ELSE 0.0 END) * {fx_rate} AS gcp_base_od,
            SUM(CASE WHEN m.component IN ('license', 'sql_license') THEN p.gcp_projected_cost ELSE 0.0 END) * {fx_rate} AS gcp_lic_od,
            STRING_AGG(DISTINCT CASE WHEN m.component IN ('license', 'sql_license') THEN COALESCE(NULLIF(m.gcp_sku_name, ''), m.component) ELSE NULL END, ', ') AS lic_name,
            {_commitment_basis_expr}                                           AS commitment_basis,
            {_aws_cost_od_est_expr}                                            AS aws_cost_od_est,
            {_od_basis_expr}                                                   AS od_basis
        FROM aws_li_catalog c
        LEFT JOIN aws_li_to_gcp_li m ON c.aws_li_key = m.aws_li_key
        LEFT JOIN gcp_projection    p ON c.aws_li_key = p.aws_li_key
                                      AND p.component IS NOT DISTINCT FROM m.component
        GROUP BY c.aws_li_key, c.product, c.operation, c.aws_amortized_cost, c.aws_region, c.gcp_region, c.is_workload, c.pricing_model, c.instance_type
        ORDER BY c.aws_amortized_cost DESC
    """).fetchall()

    # ── row filter (B5) ─────────────────────────────────────────────────────────
    # Totals are computed from SQL over ALL rows (unchanged). Only the visible
    # rows in the detail table are filtered. The disclosure line (Rule 2) tells
    # the reader why visible rows don't sum to the headline totals.
    _row_min_cost = rs_num("row_min_cost_usd", 0.0)
    _row_filter_note = ""
    if _row_min_cost > 0:
        _rows_before = len(rows)
        _all_rows_aws = sum(r[5] or 0.0 for r in rows)
        rows = [r for r in rows if (r[5] or 0.0) >= _row_min_cost]
        _rows_after = len(rows)
        if _rows_after < _rows_before:
            _hidden_count = _rows_before - _rows_after
            _hidden_aws = _all_rows_aws - sum(r[5] or 0.0 for r in rows)
            _row_filter_note = (
                f"Showing {_rows_after} of {_rows_before} services. "
                f"{_hidden_count} smaller row(s) totalling ${_hidden_aws:,.2f} "
                f"are not listed; all totals above include them."
            )

    # ── passthrough spend — logged only (see below), not a report card ───────
    passthrough_spend = (conn.execute("""
        SELECT COALESCE(SUM(c.aws_amortized_cost), 0)
        FROM aws_li_catalog c
        JOIN aws_li_to_gcp_li m USING (aws_li_key)
        WHERE m.strategy = 'passthrough' AND c.is_workload
    """).fetchone()[0]) * aws_fx_rate
    passthrough_pct = (passthrough_spend / aws_grand * 100) if aws_grand else 0
    log.info(f"Passthrough spend: {fmt(passthrough_spend)} ({passthrough_pct:.1f}% of bill)")

    # ── rate provenance coverage (P0-C) ──────────────────────────────────────
    rate_source_rows = []
    try:
        rate_source_rows = conn.execute("""
            SELECT
                COALESCE(m.rate_source, 'unknown')                     AS source,
                COUNT(*)                                                AS row_count,
                COALESCE(SUM(c.aws_amortized_cost), 0)                 AS spend
            FROM aws_li_to_gcp_li m
            JOIN aws_li_catalog c USING (aws_li_key)
            GROUP BY source
            ORDER BY spend DESC
        """).fetchall()
    except Exception as e:
        log.debug(f"rate_source column not available (old job schema): {e}")

    word_overlap_by_service = []
    try:
        word_overlap_by_service = conn.execute("""
            SELECT m.gcp_service,
                   COUNT(*)                        AS rows,
                   COALESCE(SUM(c.aws_amortized_cost), 0) AS spend
            FROM aws_li_to_gcp_li m
            JOIN aws_li_catalog c USING (aws_li_key)
            WHERE m.rate_source = 'word_overlap'
            GROUP BY m.gcp_service
            ORDER BY spend DESC
            LIMIT 10
        """).fetchall()
    except Exception as e:
        log.debug(f"word_overlap_by_service query failed: {e}")

    # ── wins/losses (top 5 each, workload only, mapped rows) ─────────────────
    wl_rows = conn.execute(f"""
        WITH agg AS (
            SELECT c.product,
                   ANY_VALUE(m.gcp_service) AS gcp_service,
                   c.aws_amortized_cost * {aws_fx_rate}     AS aws,
                   SUM(COALESCE(p.gcp_cost_1yr_cud, p.gcp_projected_cost)) * {fx_rate} AS gcp
            FROM aws_li_catalog c
            JOIN aws_li_to_gcp_li m USING (aws_li_key)
            JOIN gcp_projection    p ON p.aws_li_key = c.aws_li_key
                                    AND p.component IS NOT DISTINCT FROM m.component
            WHERE c.is_workload AND m.strategy IN ('map','break_down')
              AND c.aws_amortized_cost * {aws_fx_rate} > 5
            GROUP BY c.aws_li_key, c.product, c.aws_amortized_cost
        )
        SELECT product, gcp_service, aws, gcp, (aws - gcp) AS savings
        FROM agg WHERE savings IS NOT NULL
        ORDER BY savings DESC
    """).fetchall()

    gcp_wins  = [r for r in wl_rows if r[4] > 0][:6]
    gcp_loses = [r for r in reversed(wl_rows) if r[4] < 0][:6]

    # ── under-projection suspects (R5) ────────────────────────────────────────
    # Rows where GCP < 10% of AWS on material spend (aws > $50), strategy='map'.
    # These may reflect wrong SKU, missing components, or unit mismatch.
    # We do NOT clamp — GCP may genuinely be cheaper — but we flag them for review.
    under_proj_rows = conn.execute(f"""
        SELECT c.product,
               ANY_VALUE(m.gcp_service)      AS gcp_service,
               ANY_VALUE(m.gcp_sku_name)     AS gcp_sku,
               c.aws_amortized_cost * {aws_fx_rate}          AS aws,
               SUM(p.gcp_projected_cost) * {fx_rate}     AS gcp,
               STRING_AGG(DISTINCT m.projection_note, ' | ') AS notes
        FROM aws_li_catalog c
        JOIN aws_li_to_gcp_li m USING (aws_li_key)
        JOIN gcp_projection    p ON p.aws_li_key = c.aws_li_key
                                AND p.component IS NOT DISTINCT FROM m.component
        WHERE c.aws_amortized_cost * {aws_fx_rate} > 50
          AND m.strategy IN ('map', 'break_down')
        GROUP BY c.aws_li_key, c.product, c.aws_amortized_cost
        HAVING SUM(p.gcp_projected_cost) * {fx_rate} > 0
           AND SUM(p.gcp_projected_cost) * {fx_rate} < 0.10 * c.aws_amortized_cost * {aws_fx_rate}
        ORDER BY c.aws_amortized_cost DESC
        LIMIT 20
    """).fetchall()

    # ── build HTML ────────────────────────────────────────────────────────────
    def diff_td(aws, gcp):
        if gcp is None or aws is None:
            return '<td class="num">—</td>'
        d = aws - gcp
        if abs(d) < 0.005:
            return f'<td class="num">$0.00</td>'
        if d > 0:
            return f'<td class="num green">+{fmt(d)}</td>'
        return f'<td class="num red">−{fmt(abs(d))}</td>'

    # summary card values — compare GCP against the AWS Total (Bill), the one
    # number every reader already has in front of them, so the % badge never
    # needs a second baseline explained to make sense of it.
    def _pct(aws, gcp):
        if not aws:
            return ""
        p = (gcp - aws) / aws * 100
        s = "+" if p >= 0 else "−"
        return f"{s}{abs(p):.1f}% vs AWS"

    def _verdict_sentence(aws, od, cud1, cud3):
        """One plain-English line a reader can act on without doing any math
        themselves — pick the best GCP option and state the savings/cost
        increase in dollars and percent, plainly."""
        best_label, best_val = min(
            [("On-Demand", od), ("a 1-Year commitment", cud1), ("a 3-Year commitment", cud3)],
            key=lambda x: x[1],
        )
        if not aws:
            return "Not enough billing data to compare AWS and GCP totals."
        diff = aws - best_val
        pct = abs(diff) / aws * 100
        if abs(diff) < 0.5:
            return f"GCP costs about the same as AWS (within {fmt(abs(diff))})."
        if diff > 0:
            return f"<b style='color:#0D9D58'>GCP is cheaper</b> — you'd save {fmt(diff)}/mo ({pct:.0f}%) with {best_label}."
        return f"<b style='color:#D93025'>GCP costs more</b> — {fmt(abs(diff))}/mo ({pct:.0f}%) more, even with {best_label}."

    # outlier note (from outlier_violations.md if present)
    outlier_note = ""
    outlier_path = os.path.join(JOB_DIR, "projection-audit", "outlier_violations.md")
    if os.path.exists(outlier_path):
        with open(outlier_path) as of:
            outlier_note = of.read().strip()

    # ── wins/losses table HTML ────────────────────────────────────────────────
    def wins_table(data, header_label, color):
        if not data:
            if passthrough_spend > 0:
                return (
                    f"<p style='color:#80868B;font-size:12px'>No significant {header_label.lower()} found "
                    f"among rows with a resolved GCP price. {fmt(passthrough_spend)} of workload spend "
                    f"({passthrough_pct:.1f}% of the bill) is still passthrough (carried at AWS cost, no "
                    f"GCP-specific price resolved yet) and isn't reflected in this comparison.</p>"
                )
            return f"<p style='color:#80868B;font-size:12px'>No significant {header_label.lower()} found.</p>"
        rows_html = ""
        for product, gcp_service, aws, gcp, diff in data:
            ptype = pill_type(gcp_service, product)
            pct = abs(diff) / aws * 100 if aws else 0
            rows_html += (
                f"<tr>"
                f"<td>{pill_html(ptype)}{_html.escape(str(product or ''))}</td>"
                f"<td style='color:#5F6368;font-size:12px'>{_html.escape(str(gcp_service or ''))}</td>"
                f"<td class='num'>{fmt(aws)}</td>"
                f"<td class='num'>{fmt(gcp)}</td>"
                f"<td class='num' style='color:{color};font-weight:600'>{fmt(abs(diff))} ({pct:.1f}%)</td>"
                f"</tr>"
            )
        return (
            f"<table><tr><th>AWS Service</th><th>GCP Service</th>"
            f"<th class='num'>AWS Cost</th><th class='num'>GCP 1yr CUD</th>"
            f"<th class='num'>{header_label}</th></tr>"
            f"{rows_html}</table>"
        )

    # ── mapping confidence — logged, not shown in the customer-facing report
    # (it's implementation detail about how sure the pipeline is of its own
    # mapping choices, not something a reader needs to see to understand cost) ─
    log.info(f"Mapping confidence: {avg_conf*100:.1f}% overall average (excl. passthrough)")
    for cat, conf, cnt in cat_conf_rows:
        conf_str = "N/A (intentional passthrough)" if cat == "Passthrough" else f"{conf*100:.1f}%"
        log.info(f"  {cat}: {conf_str} avg confidence, {cnt} rows")

    # ── Section A and Section B row partitioning (Directive 2) ────────────────
    # Section A: Mapped workload rows with resolved GCP rate
    # Section B: Passthrough (no GCP equivalent, non-workload, or unpriced rate)
    sec_a_rows = []
    sec_b_rows = []

    for r in rows:
        product, operation, gcp_svc, strategy, notes, aws, od, cud1, cud3, aws_region, gcp_region, is_wl, pricing_model, instance_type, gcp_sku_name, gcp_machine_type, is_assump, aws_li_key, gcp_base_od, gcp_lic_od, lic_name, commitment_basis, aws_cost_od_est, od_basis = r
        if (aws or 0.0) < 0:
            continue

        # Invariant: Section A contains ONLY strategy in ('map', 'break_down')
        # with is_workload=True, aws > 0, and a resolved GCP On-Demand price (od is not None).
        if strategy in ("map", "break_down") and is_wl and od is not None and (aws or 0.0) > 0:
            sec_a_rows.append(r)
        else:
            sec_b_rows.append(r)

    def _render_row(r, idx, force_pt=False, active_columns=None):
        product, operation, gcp_svc, strategy, notes, aws, od, cud1, cud3, aws_region, gcp_region, is_wl, pricing_model, instance_type, gcp_sku_name, gcp_machine_type, is_assump, aws_li_key, gcp_base_od, gcp_lic_od, lic_name, commitment_basis, aws_cost_od_est, od_basis = r

        ptype  = pill_type(gcp_svc, product)
        p_html = pill_html(ptype)
        aws    = aws or 0.0
        aws_od_est = aws_cost_od_est if aws_cost_od_est is not None else aws

        is_ignore = (strategy == "ignore")
        is_pt = force_pt or (strategy == "passthrough") or (od is None and not is_ignore)

        cmp_aws = aws_od_est

        if force_pt:
            od = cmp_aws
            cud1 = cmp_aws
            cud3 = cmp_aws
            has_od = True
            has_cud1 = True
            has_cud3 = True
        else:
            has_od = od is not None
            od = od or 0.0
            has_cud1 = cud1 is not None
            has_cud3 = cud3 is not None
            cud1 = cud1 or 0.0
            cud3 = cud3 or 0.0

        is_spot = (pricing_model or "").lower() == "spot" or commitment_basis == "spot"
        spot_title = "Spot/Preemptible — CUDs not applicable; billed at Spot rate regardless of commitment"
        no_cud_title = "No Committed Use Discount rate available for this SKU — billed at On-Demand rate under a CUD commitment"
        marker = '<span class="cud-na-marker" title="{}">&dagger;</span>'

        def _cud_cell(has_cud, cud_val):
            if has_cud:
                return fmt(cud_val), ''
            title = spot_title if is_spot else no_cud_title
            if has_od:
                return fmt(od) + marker.format(_html.escape(title)), ''
            return '—', f' title="{_html.escape(title)}"'

        cud1_str, cud1_td_extra = _cud_cell(has_cud1 and not is_spot, cud1)
        cud3_str, cud3_td_extra = _cud_cell(has_cud3 and not is_spot, cud3)

        strategy_badge = ""
        if is_pt:
            strategy_badge = '<span class="strat-pt">[passthrough]</span>'
        elif is_ignore:
            strategy_badge = '<span class="strat-pt">[no GCP charge]</span>'

        op_str = _html.escape(str(operation or ""))
        region_str = _html.escape(str(gcp_region or aws_region or "—"))

        why_html, cost_tier_html = _format_why_html(notes)

        basis_tag = f' <span class="basis-tag" style="font-size:10px;color:#5F6368" title="OD basis: {_html.escape(str(od_basis))}">[{_html.escape(str(od_basis))}]</span>' if od_basis else ""

        desc_html = f'<div class="desc">'
        if op_str:
            desc_html += op_str
        desc_html += basis_tag
        desc_html += '</div>'
        desc_html += why_html
        desc_html += cost_tier_html

        has_license_modeled = (gcp_lic_od is not None and gcp_lic_od > 0.005 and bool(lic_name) and not is_pt and not is_ignore)

        is_estimated_od = (commitment_basis != "on_demand" and abs(aws_od_est - aws) > 0.005)

        if is_ignore:
            od_str   = fmt(0.0)
            cud1_str = fmt(0.0)
            cud3_str = fmt(0.0)
            diff_td  = f'<td class="num green">+{fmt(cmp_aws)}</td>' if cmp_aws > 0.005 else '<td class="num">—</td>'
        elif is_pt:
            od_str = fmt(cmp_aws)
            cud1_str = fmt(cmp_aws)
            cud3_str = fmt(cmp_aws)
            diff_td = '<td class="num">—</td>'
        elif not has_od:
            od_str   = '—'
            cud1_str = '—'
            cud3_str = '—'
            diff_td  = '<td class="num">—</td>'
        else:
            if has_license_modeled:
                base_val = gcp_base_od or 0.0
                lic_val = gcp_lic_od or 0.0
                od_str = f"{fmt(base_val)} + {fmt(lic_val)}"
                if is_assump:
                    od_str += "*"
            else:
                od_str = f"{fmt(od)}*" if is_assump else fmt(od)
            diff = cmp_aws - od
            if abs(diff) < 0.005:
                diff_td = '<td class="num">—</td>'
            elif diff > 0:
                diff_td = f'<td class="num green">+{fmt(diff)}</td>'
            else:
                diff_td = f'<td class="num red">−{fmt(abs(diff))}</td>'

        # L-4: AWS side instance + license split (Phase 2)
        raw_instance = instance_type or _short_aws_label(product) or "—"
        aws_lic_split = None
        if has_license_modeled:
            aws_lic_split = _get_aws_license_split(instance_type, lic_name, aws)

        if aws_lic_split:
            aws_base_cost, aws_lic_cost, _os_key = aws_lic_split
            aws_instance_str = _html.escape(str(raw_instance))
            aws_cost_cell = f"{fmt(aws_base_cost)} + {fmt(aws_lic_cost)}"
        elif has_license_modeled or ("license" in (notes or "").lower() and "oracle" in (product or "").lower()):
            aws_instance_str = f"{_html.escape(str(raw_instance))} <span style='font-size:10px;color:#5F6368'>(incl. OS license)</span>"
            aws_cost_cell = fmt(aws)
        else:
            aws_instance_str = _html.escape(str(raw_instance))
            if is_estimated_od:
                aws_cost_cell = (
                    f"{fmt(aws_od_est)}* "
                    f"<div style='font-size:10px;color:#5F6368'>(${fmt(aws)} billed)</div>"
                )
            else:
                aws_cost_cell = fmt(aws)

        if is_ignore:
            top_line = gcp_sku_name or gcp_svc or "Free on GCP"
            future_start_m = re.search(
                r"billing window starts (\d{4}-\d{2}-\d{2})", notes or ""
            )
            badge_text = (
                f"Free until {future_start_m.group(1)}" if future_start_m else "Free tier"
            )
            gcp_mapping_str = (
                f'{_html.escape(str(top_line))}<br>'
                f'<span class="free-tier">({_html.escape(badge_text)})</span>'
            )
        elif gcp_machine_type and not is_pt:
            gcp_mapping_str = _html.escape(str(gcp_machine_type))
        elif gcp_sku_name and not is_pt:
            gcp_mapping_str = _html.escape(str(gcp_sku_name))
        elif is_pt:
            gcp_mapping_str = "Passthrough"
        else:
            gcp_mapping_str = "—"

        row_class = ' class="row-passthrough"' if is_pt else (' class="row-free"' if is_ignore else "")

        # Build cell HTML for each active column
        def cell_svc():
            return (f"<td style='white-space:nowrap'>"
                    f"  {p_html}"
                    f"  <span style='font-size:12px'>{_html.escape(str(product or ''))}</span>"
                    f"  {strategy_badge}<br>"
                    f"  <span style='font-size:11px;color:#1A73E8'>{_html.escape(str(gcp_svc or 'N/A'))}</span>"
                    f"</td>")

        def cell_instance():
            return (f"<td style='font-size:11px;min-width:180px;max-width:260px;white-space:normal;word-break:break-word'>{aws_instance_str}</td>")

        def cell_mapping():
            return (f"<td style='font-size:11px;color:#5F6368;min-width:220px;max-width:320px;white-space:normal;word-break:break-word'>{gcp_mapping_str}</td>")

        def cell_region():
            return f"<td style='font-size:11px;color:#5F6368;white-space:nowrap'>{region_str}</td>"

        def cell_aws():
            return f'<td class="num">{aws_cost_cell}</td>'

        def cell_od():
            return f'<td class="num">{od_str}</td>'

        def cell_cud1():
            return f'<td class="num"{cud1_td_extra}>{cud1_str}</td>'

        def cell_cud3():
            return f'<td class="num"{cud3_td_extra}>{cud3_str}</td>'

        def cell_diff():
            return diff_td

        def cell_line_items():
            return "<td class='num'>1</td>"

        def cell_aws_region():
            return f"<td style='font-size:11px;color:#5F6368;white-space:nowrap'>{region_str}</td>"

        def cell_desc():
            return f'<td class="extra-col">{desc_html}</td>'

        def cell_pct():
            if not cmp_aws:
                return "<td class='num'>—</td>"
            pct = (od / cmp_aws * 100) if cmp_aws > 0 else 0
            color = "#137333" if pct <= 100 else "#D93025"
            return f"<td class='num' style='color:{color};font-weight:500'>{pct:.0f}%</td>"

        cell_builders = {
            "#": lambda: f"<td style='color:#80868B;font-size:11px'>{idx}</td>",
            "AWS Service → GCP Service": cell_svc,
            "AWS Instance": cell_instance,
            "GCP Mapping": cell_mapping,
            "Region": cell_region,
            "AWS": cell_aws,
            "GCP OD": cell_od,
            "GCP 1yr CUD": cell_cud1,
            "GCP 3yr CUD": cell_cud3,
            "Diff": cell_diff,
            "GCP as % of AWS": cell_pct,
            "Line items": cell_line_items,
            "AWS Region": cell_aws_region,
        }

        if active_columns is None:
            active_columns = _ACTIVE_COLUMNS

        cells = ""
        for key, label, _ in active_columns:
            builder = cell_builders.get(label)
            if builder:
                cells += builder()
            else:
                cells += "<td>—</td>"
        cells += cell_desc()  # Details column always shown

        main_row_html = (
            f"<tr{row_class}>"
            f"{cells}"
            f"</tr>\n"
        )

        sub_row_html = ""
        if has_license_modeled:
            aws_sub_cell = ""
            aws_ratio_note = ""
            if aws_lic_split:
                _base, a_lic, _ = aws_lic_split
                aws_sub_cell = f'<span class="plus-sign">+</span>{fmt(a_lic)}'
                aws_ratio_note = '<div style="font-size:9px;color:#80868B;font-style:italic">AWS license split estimated from AWS Price List Windows/Linux OD ratio.</div>'

            sub_cells = ""
            for _, label, _ in active_columns:
                if label == "#":
                    sub_cells += "<td></td>"
                elif label == "AWS Service → GCP Service":
                    sub_cells += f'<td><span class="plus-sign">+</span> {_html.escape(str(lic_name))}{aws_ratio_note}</td>'
                elif label == "AWS Instance":
                    sub_cells += "<td></td>"
                elif label == "GCP Mapping":
                    sub_cells += "<td></td>"
                elif label == "Region":
                    sub_cells += f'<td style="color:#5F6368">{region_str}</td>'
                elif label == "AWS":
                    sub_cells += f'<td class="num">{aws_sub_cell}</td>'
                elif label == "GCP OD":
                    sub_cells += f'<td class="num"><span class="plus-sign">+</span>{fmt(gcp_lic_od)}</td>'
                elif label in ("GCP 1yr CUD", "GCP 3yr CUD"):
                    sub_cells += '<td class="num"></td>'
                elif label == "Diff":
                    sub_cells += '<td class="num"></td>'
                elif label == "GCP as % of AWS":
                    sub_cells += '<td class="num"></td>'
                elif label == "Line items":
                    sub_cells += '<td class="num"></td>'
                elif label == "AWS Region":
                    sub_cells += f'<td style="color:#5F6368">{region_str}</td>'
                else:
                    sub_cells += "<td></td>"
            sub_cells += '<td class="extra-col"></td>'

            sub_row_html = (
                f'<tr class="row-license-sub">'
                f'{sub_cells}'
                f'</tr>\n'
            )

        return main_row_html + sub_row_html

    # ── Render Section A Rows & Subtotal ──────────────────────────────────────
    sec_a_html = ""
    sec_a_aws = 0.0
    sec_a_gcp_od = 0.0
    sec_a_gcp_1yr = 0.0
    sec_a_gcp_3yr = 0.0

    idx = 1
    for r in sec_a_rows:
        aws = r[5] or 0.0
        od = r[6] if r[6] is not None else 0.0
        c1 = r[7] if r[7] is not None else od
        c3 = r[8] if r[8] is not None else od
        is_spot = (r[12] or "").lower() == "spot" or (r[21] or "").lower() == "spot"
        if is_spot:
            c1 = od
            c3 = od
        aws_od_est = r[22] if r[22] is not None else aws
        sec_a_aws += aws_od_est
        sec_a_gcp_od += od
        sec_a_gcp_1yr += c1
        sec_a_gcp_3yr += c3
        sec_a_html += _render_row(r, idx, force_pt=False, active_columns=_ACTIVE_COLUMNS)
        idx += 1

    diff_sec_a = sec_a_aws - sec_a_gcp_od
    diff_sec_a_class = "green" if diff_sec_a > 0 else "red" if diff_sec_a < 0 else ""
    diff_sec_a_str = ("—" if abs(diff_sec_a) < 0.005
                      else (f"+{fmt(diff_sec_a)}" if diff_sec_a > 0 else f"−{fmt(abs(diff_sec_a))}"))
    def _render_summary_row(row_class, label_text, aws_val, od_val, c1_val, c3_val, diff_str, diff_cls, pct_str):
        # Count leading non-numeric columns to span with label_text
        leading_cols = 0
        for _, col_label, _ in _ACTIVE_COLUMNS:
            if col_label in ("#", "AWS Service → GCP Service", "AWS Instance", "GCP Mapping", "Region"):
                leading_cols += 1
            else:
                break
        leading_cols = max(1, leading_cols)

        row_html = f'<tr class="{row_class}">'
        row_html += f'<td colspan="{leading_cols}" style="font-weight:600">{label_text}</td>'

        # Render remaining columns
        for _, col_label, _ in _ACTIVE_COLUMNS[leading_cols:]:
            if col_label == "AWS":
                row_html += f'<td class="num">{fmt(aws_val)}</td>'
            elif col_label == "GCP OD":
                row_html += f'<td class="num">{fmt(od_val)}</td>'
            elif col_label == "GCP 1yr CUD":
                row_html += f'<td class="num">{fmt(c1_val)}</td>'
            elif col_label == "GCP 3yr CUD":
                row_html += f'<td class="num">{fmt(c3_val)}</td>'
            elif col_label == "Diff":
                cls_attr = f' class="num {diff_cls}"' if diff_cls else ' class="num"'
                row_html += f'<td{cls_attr} style="font-weight:600">{diff_str}</td>'
            elif col_label == "GCP as % of AWS":
                row_html += f'<td class="num" style="font-weight:600">{pct_str}</td>'
            elif col_label == "Line items":
                row_html += '<td class="num"></td>'
            elif col_label == "AWS Region":
                row_html += '<td></td>'
            else:
                row_html += '<td></td>'

        row_html += '<td class="extra-col"></td>'
        row_html += '</tr>\n'
        return row_html

    pct_sec_a_badge = f" ({((sec_a_gcp_od - sec_a_aws) / sec_a_aws * 100):+.1f}%)" if sec_a_aws > 0 else ""
    pct_sec_a_cell = f"{((sec_a_gcp_od / sec_a_aws * 100)):.0f}%" if sec_a_aws > 0 else "—"

    subtotal_a_html = _render_summary_row(
        row_class="subtotal-row",
        label_text=f"SUBTOTAL SECTION A (MAPPED WORKLOAD){pct_sec_a_badge}",
        aws_val=sec_a_aws,
        od_val=sec_a_gcp_od,
        c1_val=sec_a_gcp_1yr,
        c3_val=sec_a_gcp_3yr,
        diff_str=diff_sec_a_str,
        diff_cls=diff_sec_a_class,
        pct_str=pct_sec_a_cell,
    )

    # ── Render Section B Rows & Subtotal ──────────────────────────────────────
    sec_b_html = ""
    sec_b_aws = 0.0
    sec_b_gcp_od = 0.0
    sec_b_gcp_1yr = 0.0
    sec_b_gcp_3yr = 0.0

    for r in sec_b_rows:
        aws = r[5] or 0.0
        aws_od_est = r[22] if r[22] is not None else aws
        sec_b_aws += aws_od_est
        sec_b_gcp_od += aws_od_est
        sec_b_gcp_1yr += aws_od_est
        sec_b_gcp_3yr += aws_od_est
        sec_b_html += _render_row(r, idx, force_pt=True, active_columns=_ACTIVE_COLUMNS)
        idx += 1

    subtotal_b_html = _render_summary_row(
        row_class="subtotal-row",
        label_text="SUBTOTAL SECTION B (PASSTHROUGH / NON-WORKLOAD / UNPRICED)",
        aws_val=sec_b_aws,
        od_val=sec_b_gcp_od,
        c1_val=sec_b_gcp_1yr,
        c3_val=sec_b_gcp_3yr,
        diff_str="$0.00",
        diff_cls="",
        pct_str="100%" if sec_b_aws > 0 else "—",
    )

    # ── Grand Totals (Section A + Section B strictly footing) ─────────────────
    gcp_grand_od  = sec_a_gcp_od + sec_b_gcp_od
    gcp_grand_1yr = sec_a_gcp_1yr + sec_b_gcp_1yr
    gcp_grand_3yr = sec_a_gcp_3yr + sec_b_gcp_3yr
    aws_gross     = sec_a_aws + sec_b_aws

    diff_tot = aws_gross - gcp_grand_od
    diff_tot_class = "green" if diff_tot > 0 else "red" if diff_tot < 0 else ""
    diff_tot_str = ("—" if abs(diff_tot) < 0.005
                    else (f"+{fmt(diff_tot)}" if diff_tot > 0 else f"−{fmt(abs(diff_tot))}"))
    pct_grand = f"{((gcp_grand_od / aws_gross * 100)):.0f}%" if aws_gross > 0 else "—"

    grand_total_row_html = _render_summary_row(
        row_class="total-row",
        label_text="GRAND TOTAL (LIST PRICES)",
        aws_val=aws_gross,
        od_val=gcp_grand_od,
        c1_val=gcp_grand_1yr,
        c3_val=gcp_grand_3yr,
        diff_str=diff_tot_str,
        diff_cls=diff_tot_class,
        pct_str=pct_grand,
    )

    detail_rows_html = (
        f'<tr class="section-header-row"><th colspan="{_NUM_COLS}">SECTION A — MAPPED (REAL GCP EQUIVALENT)</th></tr>\n'
        + sec_a_html
        + subtotal_a_html
        + f'<tr class="section-header-row"><th colspan="{_NUM_COLS}">SECTION B — PASSTHROUGH / NO DIRECT GCP EQUIVALENT (CARRIED 1:1)</th></tr>\n'
        + sec_b_html
        + subtotal_b_html
        + grand_total_row_html
    )

    # ── discounts by service block (H-3, Section C) ───────────────────────────
    discount_rows = []
    try:
        discount_rows = conn.execute(f"""
            SELECT
                COALESCE(c.product, 'Discounts & Credits') AS service,
                STRING_AGG(DISTINCT COALESCE(NULLIF(c.line_item_type, ''), c.mechanic_group), ', ') AS discount_types,
                COUNT(*)                                   AS item_count,
                SUM(ABS(c.aws_amortized_cost)) * {aws_fx_rate}             AS discount_amount
            FROM aws_li_catalog c
            WHERE c.aws_amortized_cost < 0
            GROUP BY c.product
            HAVING discount_amount > 0.005
            ORDER BY discount_amount DESC
        """).fetchall()
    except Exception as e:
        log.warning(f"Could not query discounts by service: {e}")

    # L-9: Compute total discounts authoritatively from aws_discounts (sum of all negative rows)
    # to avoid dropping tiny sub-cent credits in the net total box.
    display_discounts = aws_discounts

    discounts_block_html = ""
    if rs_bool("section_discounts") and (display_discounts > 0.5 and discount_rows):
        disc_table_rows = ""
        for d_idx, (d_svc, d_types, d_count, d_amt) in enumerate(discount_rows, 1):
            disc_table_rows += (
                f"<tr>"
                f"<td style='color:#80868B;font-size:11px'>{d_idx}</td>"
                f"<td>{_html.escape(str(d_svc))}</td>"
                f"<td style='color:#5F6368;font-size:11px'>{_html.escape(str(d_types or 'Discount / Credit'))}</td>"
                f"<td class='num'>{d_count:,}</td>"
                f"<td class='num green' style='font-weight:600'>−{fmt(d_amt)}</td>"
                f"</tr>\n"
            )
        discounts_block_html = f"""
<div class="discounts-section" style="margin-top:28px">
  <h3>Discounts by Service (Section C)</h3>
  <p class="section-meta">
    AWS discounts, credits, and Savings Plans / EDP reductions applied to this bill, grouped by service.
    These are excluded from the headline table above to keep the comparison strictly <b>AWS On-Demand List vs GCP On-Demand List</b>.
  </p>
  <div class="table-scroll"><table>
    <tr>
      <th>#</th>
      <th>AWS Service</th>
      <th>Discount Type / Description</th>
      <th class="num">Line Items</th>
      <th class="num">Discount Amount</th>
    </tr>
    {disc_table_rows}
    <tr class="total-row">
      <td colspan="4" style="font-weight:600">TOTAL AWS DISCOUNTS &amp; CREDITS</td>
      <td class="num green" style="font-weight:600">−{fmt(display_discounts)}</td>
    </tr>
  </table></div>

  <div class="net-spend-box" style="background:#F8F9FA;border:1px solid #DADCE0;border-radius:8px;padding:16px;margin:18px 0">
    <div style="font-size:12px;font-weight:600;color:#5F6368;text-transform:uppercase;letter-spacing:0.5px">Customer Net AWS Total ("What you actually pay AWS today")</div>
    <div style="font-size:22px;font-weight:700;color:#202124;margin-top:4px">
      {fmt(aws_grand)} / mo
      <span style="font-size:13px;font-weight:400;color:#5F6368">
        (AWS List Total {fmt(aws_gross)} − Discounts &amp; Credits {fmt(display_discounts)})
      </span>
    </div>
    <p style="font-size:12px;color:#5F6368;margin-top:6px;margin-bottom:0">
      * Note: The headline table compares baseline list prices ({fmt(aws_gross)} vs {fmt(gcp_grand_od)}) so you can evaluate true unit economics before negotiating GCP commitments.
    </p>
  </div>
</div>
"""
    elif display_discounts > 0.5:
        discounts_block_html = f"""
<div class="discounts-section" style="margin-top:28px">
  <div class="net-spend-box" style="background:#F8F9FA;border:1px solid #DADCE0;border-radius:8px;padding:16px;margin:18px 0">
    <div style="font-size:12px;font-weight:600;color:#5F6368;text-transform:uppercase;letter-spacing:0.5px">Customer Net AWS Total ("What you actually pay AWS today")</div>
    <div style="font-size:22px;font-weight:700;color:#202124;margin-top:4px">
      {fmt(aws_grand)} / mo
      <span style="font-size:13px;font-weight:400;color:#5F6368">
        (AWS List Total {fmt(aws_gross)} − Discounts &amp; Credits {fmt(display_discounts)})
      </span>
    </div>
  </div>
</div>
"""
    else:
        discounts_block_html = f"""
<div class="discounts-section" style="margin-top:28px">
  <div class="net-spend-box" style="background:#F8F9FA;border:1px solid #DADCE0;border-radius:8px;padding:16px;margin:18px 0">
    <div style="font-size:12px;font-weight:600;color:#5F6368;text-transform:uppercase;letter-spacing:0.5px">Customer Net AWS Total</div>
    <div style="font-size:22px;font-weight:700;color:#202124;margin-top:4px">
      {fmt(aws_grand)} / mo
    </div>
    <p style="font-size:12px;color:#5F6368;margin-top:6px;margin-bottom:0">
      No customer discounts or credits detected on this bill. AWS List Total equals Net AWS Total.
    </p>
  </div>
</div>
"""
    # ── AR-5 Assumptions Section ──
    assumptions_block_html = ""
    if rs_bool("section_assumptions"):
        try:
            snapshot_data = {}
            for cand in [os.environ.get("JOB_DIR"), "."]:
                if cand and os.path.exists(os.path.join(cand, "assumptions.json")):
                    try:
                        with open(os.path.join(cand, "assumptions.json"), encoding="utf-8") as f:
                            snapshot_data = json.load(f)
                        break
                    except Exception:
                        pass
            if not snapshot_data:
                try:
                    with open(os.path.join(_DATA_DIR, "assumption-defaults.json"), encoding="utf-8") as f:
                        snapshot_data = json.load(f)
                except Exception:
                    pass

            category_labels = {
                "compute_sizing": "Compute Sizing",
                "licensing": "Licensing",
                "storage": "Storage",
                "networking": "Networking",
                "commitment": "Commitment",
                "pricing_policy": "Pricing Policy",
                "other": "Other",
            }
            ordered_cats = ["compute_sizing", "licensing", "storage", "networking", "commitment", "pricing_policy", "other"]

            key_cats = snapshot_data.get("categories", {})
            if "items" in snapshot_data and isinstance(snapshot_data["items"], list):
                for it in snapshot_data["items"]:
                    if isinstance(it, dict) and "key" in it and "category" in it:
                        key_cats[it["key"]] = it["category"]

            def infer_cat(b):
                bl = b.lower()
                for k, c in key_cats.items():
                    if k in bl:
                        return c
                if any(w in bl for w in ["vcpu", "ram", "cpu", "compute", "dpu", "acu", "shape", "instance"]):
                    return "compute_sizing"
                if any(w in bl for w in ["license", "licence", "os", "byol", "oracle", "suse", "rhel"]):
                    return "licensing"
                if any(w in bl for w in ["storage", "disk", "iops", "throughput", "pd-", "volume"]):
                    return "storage"
                if any(w in bl for w in ["network", "nat", "egress", "bandwidth", "ip"]):
                    return "networking"
                if any(w in bl for w in ["cud", "sp", "ri", "discount", "spot", "savings plan", "commitment"]):
                    return "commitment"
                if any(w in bl for w in ["currency", "vintage", "policy", "on-demand"]):
                    return "pricing_policy"
                return "other"

            assump_rows_html = ""
            if "is_assumption" in _m2g_cols:
                assump_rows = conn.execute("""
                    SELECT DISTINCT
                        COALESCE(NULLIF(m.assumption_note, ''), 'Pricing assumption applied') AS basis,
                        c.product AS service,
                        COUNT(*) AS row_cnt
                    FROM aws_li_to_gcp_li m
                    JOIN aws_li_catalog c USING (aws_li_key)
                    WHERE m.is_assumption = TRUE
                    GROUP BY basis, c.product
                    ORDER BY basis, c.product
                """).fetchall()
                if assump_rows:
                    from collections import defaultdict
                    grouped_assumptions = defaultdict(list)
                    for basis, svc, cnt in assump_rows:
                        grouped_assumptions[basis].append(f"{svc} ({cnt} row{'s' if cnt != 1 else ''})")

                    cat_groups = {c: [] for c in ordered_cats}
                    for basis, svcs in grouped_assumptions.items():
                        c = infer_cat(basis)
                        if c not in cat_groups:
                            c = "other"
                        cat_groups[c].append((basis, svcs))

                    assump_table_rows = []
                    item_idx = 1
                    for c in ordered_cats:
                        items = cat_groups[c]
                        if not items:
                            continue
                        cat_title = category_labels.get(c, c.replace('_', ' ').title())
                        assump_table_rows.append(f"""
                        <tr style="background: rgba(255,255,255,0.04)">
                          <td colspan="3" style="font-weight:700; color:#00C2BB; padding: 8px 12px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em;">
                            {_html.escape(cat_title)} ({len(items)})
                          </td>
                        </tr>
                        """)
                        for basis, svcs in items:
                            assump_table_rows.append(f"""
                            <tr>
                              <td style="font-weight:600">*{item_idx}</td>
                              <td><b>{_html.escape(basis)}</b></td>
                              <td>{_html.escape(', '.join(svcs))}</td>
                            </tr>
                            """)
                            item_idx += 1
                    assump_rows_html = "\n".join(assump_table_rows)

            if assump_rows_html:
                assumptions_block_html = f"""
<div class="assumptions-section" style="margin-top:28px">
  <h3>* Pricing Assumptions</h3>
  <p class="section-meta">
    Specific line items in this bill are projected based on documented sizing or ratio assumptions.
    Rows carrying assumptions are grouped by category below.
  </p>
  <div class="table-scroll"><table>
    <tr>
      <th style="width:40px">#</th>
      <th>Assumption &amp; Basis</th>
      <th>Applied Details</th>
    </tr>
    {assump_rows_html}
  </table></div>
</div>
"""
        except Exception as e:
            log.warning(f"Could not build assumptions section: {e}")

    # ── summary cards (M-3: compare Section A GCP against Section A AWS mapped baseline) ──
    def card(label, value, sub="", accent=False):
        a = ' card-accent' if accent else ''
        return (f'<div class="card{a}">'
                f'<div class="card-label">{label}</div>'
                f'<div class="card-value">{value}</div>'
                f'{"<div class=card-sub>" + sub + "</div>" if sub else ""}'
                f'</div>')

    aws_total_sub = "Current monthly AWS spend (no discounts)"
    if display_discounts > 0.5:
        aws_total_sub = f"Net bill: {fmt(aws_grand)} after {fmt(display_discounts)} discounts"

    cards_html = (
        card("AWS List Total", fmt(aws_gross), aws_total_sub, accent=True) +
        card("AWS Mapped Cost", fmt(sec_a_aws), "Section A workload baseline priced to GCP") +
        card("GCP On-Demand", fmt(sec_a_gcp_od), _pct(sec_a_aws, sec_a_gcp_od)) +
        card("GCP 1-Year CUD", fmt(sec_a_gcp_1yr), _pct(sec_a_aws, sec_a_gcp_1yr)) +
        card("GCP 3-Year CUD", fmt(sec_a_gcp_3yr), _pct(sec_a_aws, sec_a_gcp_3yr))
    )

    # ── assemble page ─────────────────────────────────────────────────────────
    # validation_notes and outlier_note are logged but not shown in the report

    # ── rate provenance — logged, not shown in the customer-facing report.
    # This is pipeline-internal detail (how each row's GCP rate was resolved:
    # exact SKU match vs. fuzzy word-overlap vs. no rate found) — useful for
    # someone auditing the pipeline, not for a reader trying to understand
    # what they'll pay. Full breakdown goes to stdout/job logs instead. ──────
    if rate_source_rows:
        total_rows_rs  = sum(r[1] for r in rate_source_rows)
        total_spend_rs = sum(r[2] for r in rate_source_rows)
        log.info("Rate provenance coverage:")
        for source, row_cnt, spend in rate_source_rows:
            row_pct   = 100.0 * row_cnt  / total_rows_rs  if total_rows_rs  else 0
            spend_pct = 100.0 * spend    / total_spend_rs if total_spend_rs else 0
            log.info(f"  {source}: {row_cnt:,} rows ({row_pct:.1f}%), ${spend:,.2f} spend ({spend_pct:.1f}%)")
        if word_overlap_by_service:
            log.info("  Word-overlap resolved rows by service (lowest-trust matches — verify SKU accuracy):")
            for svc, cnt, spend in word_overlap_by_service:
                log.info(f"    {svc}: {cnt} rows, {fmt(spend)}")

    wins_html  = wins_table(gcp_wins,  "GCP Savings", "#0D9D58")
    loses_html = wins_table(gcp_loses, "Extra Cost on GCP", "#D93025")

    # Gated by the section_wins_losses report setting. The tables above are
    # still computed either way — hiding a section must never change a number.
    # Customer-facing coverage statement, gated by the section_coverage report
    # setting. Deliberately neutral: it says how much workload spend was priced
    # against a specific GCP SKU and why the rest is carried at AWS cost.
    # Only INTENTIONAL passthrough reasons are named. Defect reasons
    # (unresolved_sku, llm_dropped, ...) are our problem, not the reader's --
    # they belong in the admin Job Health panel and never in a customer report.
    coverage_block = ""
    if rs_bool("section_coverage", True):
        try:
            _cov = conn.execute("""
                WITH per_key AS (
                    SELECT c.aws_li_key,
                           ANY_VALUE(c.aws_amortized_cost) AS aws,
                           MAX(CASE WHEN m.strategy IN ('map','break_down') THEN 1 ELSE 0 END) AS has_map,
                           MAX(CASE WHEN m.strategy = 'passthrough' THEN 1 ELSE 0 END) AS has_pt
                    FROM aws_li_catalog c
                    LEFT JOIN aws_li_to_gcp_li m USING (aws_li_key)
                    WHERE c.is_workload
                    GROUP BY c.aws_li_key)
                SELECT COALESCE(SUM(aws), 0),
                       COALESCE(SUM(CASE WHEN has_map = 1 THEN aws END), 0),
                       COALESCE(SUM(CASE WHEN has_map = 0 AND has_pt = 1 THEN aws END), 0)
                FROM per_key
            """).fetchone()
            _tot, _mapped, _pt = (float(x or 0.0) for x in _cov)
            if _tot > 0:
                _pct = _mapped / _tot * 100.0
                _reasons = conn.execute("""
                    WITH per_key AS (
                        SELECT c.aws_li_key,
                               ANY_VALUE(c.aws_amortized_cost) AS aws,
                               ANY_VALUE(m.passthrough_reason) AS reason,
                               MAX(CASE WHEN m.strategy IN ('map','break_down') THEN 1 ELSE 0 END) AS has_map
                        FROM aws_li_catalog c
                        LEFT JOIN aws_li_to_gcp_li m USING (aws_li_key)
                        WHERE c.is_workload AND m.strategy = 'passthrough'
                        GROUP BY c.aws_li_key)
                    SELECT reason, SUM(aws) FROM per_key
                    WHERE has_map = 0 AND reason IS NOT NULL
                    GROUP BY reason ORDER BY 2 DESC
                """).fetchall()
                _named = []
                for _r, _usd in _reasons:
                    _meta = _PT_REASONS.get(_r) or {}
                    if _meta.get("kind") == "intentional" and _meta.get("label"):
                        _named.append(_meta["label"].lower())
                _why = ""
                if _named:
                    _uniq = list(dict.fromkeys(_named))[:3]
                    _why = " Reasons given: " + ", ".join(_uniq) + "."
                # Three buckets that always sum to the workload total: priced
                # against a SKU, carried at AWS cost, and anything else. The
                # third is stated rather than left as a silent remainder --
                # a reader must be able to add the sentence up.
                _other = _tot - _mapped - _pt
                _parts = [
                    f"{_pct:.0f}% of your AWS workload spend "
                    f"({fmt(_mapped * aws_fx_rate)} of {fmt(_tot * aws_fx_rate)}) "
                    "was priced against a specific GCP SKU."
                ]
                if _pt > 0.005:
                    _parts.append(
                        f"{fmt(_pt * aws_fx_rate)} is carried at its AWS cost because no "
                        "equivalent GCP charge could be priced directly." + _why)
                if _other > 0.005:
                    _parts.append(
                        f"The remaining {fmt(_other * aws_fx_rate)} is included in the "
                        "totals above at its AWS cost.")

                # OD basis fallback disclosure (WORKORDER_od_vs_od_sp_ri)
                fb_cnt = sum(1 for r in rows if r[23] == 'amortized_fallback')
                if fb_cnt > 0:
                    fb_spend = sum((r[5] or 0.0) for r in rows if r[23] == 'amortized_fallback')
                    _parts.append(
                        f"Note: {fb_cnt} line item(s) totalling {fmt(fb_spend)} had no resolvable public On-Demand rate or commitment discount and fell back to billed amortized cost (amortized_fallback)."
                    )
                # Reconstructed-from-default-rate disclosure: these carry an ESTIMATED
                # OD cost built from a portfolio-default SP/RI/Spot discount, not a
                # per-commitment resolved rate, so the AWS OD side is an estimate.
                rd_cnt = sum(1 for r in rows if r[23] == 'reconstructed_default')
                if rd_cnt > 0:
                    rd_spend = sum((r[5] or 0.0) for r in rows if r[23] == 'reconstructed_default')
                    _parts.append(
                        f"Note: {rd_cnt} line item(s) totalling {fmt(rd_spend)} had no public On-Demand rate; their AWS On-Demand cost was reconstructed from the configured default SP/RI/Spot discount (reconstructed_default) and is an estimate."
                    )

                coverage_block = (
                    '<h2>Coverage</h2>'
                    '<p class="section-meta">' + " ".join(_parts) + '</p>'
                )
        except Exception as _ce:
            log.warning(f"coverage section skipped: {_ce}")

    wins_losses_block = ""
    if rs_bool("section_wins_losses"):
        wins_losses_block = (
            "<h2>Where GCP Wins &amp; Loses</h2>"
            '<p class="section-meta">Top services where GCP is cheaper / more '
            "expensive than current AWS spend (1-Year CUD vs AWS amortized, "
            "workload rows only).</p>"
            '<h3 style="color:#0D9D58">&#9660; GCP Savings Opportunities</h3>'
            + wins_html
            + '<h3 style="color:#D93025">&#9650; GCP Cost Increases</h3>'
            + loses_html
        )

    # Surface the already-computed under_proj_rows (GCP < 10% of AWS on mapped,
    # material-spend rows) instead of discarding them — a customer-facing flag
    # that a mapping may need review is more useful than logging it only where
    # nobody but an engineer will ever see it.
    if under_proj_rows:
        _up_rows_html = ""
        for product, gcp_service, gcp_sku, aws, gcp, notes in under_proj_rows:
            pct = (gcp / aws * 100) if aws else 0
            _up_rows_html += (
                f"<tr>"
                f"<td>{_html.escape(str(product or ''))}</td>"
                f"<td style='color:#5F6368;font-size:12px'>{_html.escape(str(gcp_service or ''))} "
                f"{_html.escape(str(gcp_sku or ''))}</td>"
                f"<td class='num'>{fmt(aws)}</td>"
                f"<td class='num'>{fmt(gcp)}</td>"
                f"<td class='num' style='color:#D93025;font-weight:600'>{pct:.1f}%</td>"
                f"</tr>"
            )
        under_proj_html = (
            '<h3 style="color:#F9AB00">&#9888; Flagged for Review</h3>'
            '<p class="section-meta">Mapped rows where the GCP estimate is under 10% of the AWS cost on '
            '&gt;$50/mo AWS spend — GCP may genuinely be far cheaper here, but this ratio also matches the '
            'signature of a wrong SKU, a missing billing component, or a unit mismatch. Worth a manual check '
            'before relying on these rows.</p>'
            '<table><tr><th>AWS Service</th><th>GCP Target</th>'
            "<th class='num'>AWS Cost</th><th class='num'>GCP Cost</th>"
            "<th class='num'>GCP as % of AWS</th></tr>"
            f"{_up_rows_html}</table>"
        )
    else:
        under_proj_html = ""

    # One plain-language line instead of a table + a paragraph explaining a
    # 0.5% tolerance — a reader just needs to know "does GCP give me at least
    # as much compute as I have today," not the tolerance math behind it.
    # Full numbers are still logged (see stdout) for anyone who wants to audit.
    if aws_vcpu == 0 and gcp_vcpu == 0:
        capacity_html = ('<p class="section-meta">Capacity check: instance specs not available in this '
                          'bill export, so vCPU/memory coverage could not be checked.</p>')
    elif vcpu_ok and ram_ok:
        capacity_html = (f'<p class="section-meta">&#10003; GCP matches or exceeds your AWS capacity — '
                          f'{gcp_vcpu:,.0f} vCPU / {gcp_ram:,.0f} GB RAM provisioned vs '
                          f'{aws_vcpu:,.0f} vCPU / {aws_ram:,.0f} GB RAM today.</p>')
    else:
        short = []
        if not vcpu_ok:
            short.append(f"vCPU ({gcp_vcpu:,.0f} vs {aws_vcpu:,.0f} needed)")
        if not ram_ok:
            short.append(f"memory ({gcp_ram:,.0f} GB vs {aws_ram:,.0f} GB needed)")
        capacity_html = (f'<p class="warn-box">&#9888; GCP capacity falls short on {" and ".join(short)} — '
                          f'review the affected rows before finalizing sizing.</p>')
    log.info(f"Capacity check: AWS {aws_vcpu:,.1f} vCPU / {aws_ram:,.1f} GB RAM vs "
          f"GCP {gcp_vcpu:,.1f} vCPU / {gcp_ram:,.1f} GB RAM "
          f"(vcpu_ok={vcpu_ok}, ram_ok={ram_ok})")

    # Real Facets.cloud logo SVG (from frontend/src/assets/facets-logo-full.svg)
    _FC_LOGO_FULL = """<svg width="119" height="20" viewBox="0 0 119 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Facets.cloud">
<path d="M37.3286 16V6.20679H43.704V7.58704H38.8797V10.5316H43.1782V11.9118H38.8797V16H37.3286ZM46.4592 16.1577C45.9772 16.1577 45.5522 16.0745 45.1841 15.908C44.8248 15.7327 44.5444 15.4961 44.3428 15.1981C44.1412 14.8914 44.0405 14.5321 44.0405 14.1202C44.0405 13.7346 44.1237 13.3885 44.2902 13.0818C44.4655 12.775 44.7328 12.5165 45.0921 12.3062C45.4514 12.0959 45.9027 11.9469 46.446 11.8592L48.9174 11.4517V12.6217L46.7352 13.0029C46.3409 13.073 46.0517 13.2001 45.8677 13.3841C45.6836 13.5594 45.5916 13.7872 45.5916 14.0676C45.5916 14.3393 45.6924 14.5628 45.8939 14.7381C46.1043 14.9046 46.3716 14.9878 46.6958 14.9878C47.0989 14.9878 47.4495 14.9002 47.7474 14.7249C48.0541 14.5496 48.2908 14.3174 48.4573 14.0282C48.6238 13.7303 48.707 13.4016 48.707 13.0423V11.2151C48.707 10.8646 48.5756 10.5798 48.3127 10.3607C48.0585 10.1328 47.7168 10.0189 47.2873 10.0189C46.893 10.0189 46.5468 10.1241 46.2489 10.3344C45.9597 10.536 45.745 10.7989 45.6048 11.1231L44.3691 10.5053C44.5006 10.1547 44.7153 9.84803 45.0132 9.58512C45.3112 9.31345 45.6573 9.10313 46.0517 8.95415C46.4548 8.80517 46.8798 8.73068 47.3268 8.73068C47.8876 8.73068 48.3828 8.83584 48.8122 9.04617C49.2504 9.25649 49.5878 9.55007 49.8244 9.9269C50.0698 10.295 50.1924 10.7244 50.1924 11.2151V16H48.7728V14.7118L49.0751 14.7512C48.9086 15.0404 48.6939 15.2902 48.431 15.5005C48.1768 15.7108 47.8833 15.8729 47.5502 15.9869C47.226 16.1008 46.8623 16.1577 46.4592 16.1577ZM55.1104 16.1577C54.4006 16.1577 53.7696 15.9956 53.2175 15.6714C52.6742 15.3384 52.2404 14.8914 51.9161 14.3306C51.6006 13.7697 51.4429 13.1343 51.4429 12.4245C51.4429 11.7234 51.6006 11.0924 51.9161 10.5316C52.2316 9.97072 52.6654 9.53254 53.2175 9.21705C53.7696 8.89281 54.4006 8.73068 55.1104 8.73068C55.5924 8.73068 56.0437 8.81832 56.4644 8.99358C56.885 9.16009 57.2487 9.39232 57.5554 9.69028C57.8709 9.98824 58.1031 10.3344 58.2521 10.7288L56.9507 11.3334C56.8018 10.9654 56.5608 10.6718 56.2278 10.4527C55.9035 10.2249 55.5311 10.1109 55.1104 10.1109C54.7073 10.1109 54.3436 10.2117 54.0194 10.4133C53.7039 10.6061 53.4541 10.8821 53.2701 11.2414C53.086 11.592 52.994 11.9907 52.994 12.4376C52.994 12.8846 53.086 13.2877 53.2701 13.647C53.4541 13.9975 53.7039 14.2736 54.0194 14.4752C54.3436 14.6767 54.7073 14.7775 55.1104 14.7775C55.5398 14.7775 55.9123 14.6679 56.2278 14.4489C56.552 14.221 56.793 13.9187 56.9507 13.5418L58.2521 14.1597C58.1119 14.5365 57.8841 14.8783 57.5686 15.185C57.2618 15.483 56.8982 15.7196 56.4775 15.8948C56.0569 16.0701 55.6012 16.1577 55.1104 16.1577ZM62.8457 16.1577C62.1358 16.1577 61.5048 15.9956 60.9527 15.6714C60.4094 15.3384 59.9844 14.8914 59.6777 14.3306C59.3709 13.7609 59.2176 13.1256 59.2176 12.4245C59.2176 11.7059 59.3709 11.0705 59.6777 10.5184C59.9931 9.96633 60.4138 9.53254 60.9396 9.21705C61.4654 8.89281 62.0613 8.73068 62.7274 8.73068C63.2619 8.73068 63.7395 8.8227 64.1602 9.00673C64.5808 9.19076 64.9358 9.44491 65.225 9.76915C65.5141 10.0846 65.7332 10.4483 65.8822 10.8602C66.04 11.2721 66.1188 11.7103 66.1188 12.1747C66.1188 12.2887 66.1144 12.407 66.1057 12.5297C66.0969 12.6523 66.0794 12.7663 66.0531 12.8714H60.3875V11.6884H65.2118L64.502 12.2273C64.5896 11.7979 64.5589 11.4167 64.4099 11.0837C64.2697 10.7419 64.0506 10.4746 63.7527 10.2818C63.4635 10.0803 63.1217 9.97948 62.7274 9.97948C62.333 9.97948 61.9825 10.0803 61.6757 10.2818C61.369 10.4746 61.1324 10.755 60.9659 11.1231C60.7994 11.4824 60.7337 11.9206 60.7687 12.4376C60.7249 12.9196 60.7906 13.3403 60.9659 13.6996C61.1499 14.0589 61.4041 14.3393 61.7283 14.5409C62.0613 14.7424 62.4382 14.8432 62.8588 14.8432C63.2882 14.8432 63.6519 14.7468 63.9499 14.554C64.2566 14.3612 64.4976 14.1115 64.6729 13.8047L65.8822 14.3963C65.742 14.7293 65.5229 15.0316 65.225 15.3033C64.9358 15.5662 64.5852 15.7765 64.1733 15.9343C63.7702 16.0833 63.3277 16.1577 62.8457 16.1577ZM70.4473 16.0789C69.7024 16.0789 69.124 15.8685 68.7121 15.4479C68.3003 15.0273 68.0943 14.4357 68.0943 13.6733V10.2292H66.8455V8.88842H67.0427C67.3757 8.88842 67.6342 8.79203 67.8183 8.59923C68.0023 8.40643 68.0943 8.14353 68.0943 7.81051V7.25841H69.5797V8.88842H71.1966V10.2292H69.5797V13.6076C69.5797 13.8529 69.6192 14.0633 69.698 14.2385C69.7769 14.405 69.904 14.5365 70.0792 14.6329C70.2545 14.7205 70.4824 14.7643 70.7628 14.7643C70.8329 14.7643 70.9118 14.76 70.9994 14.7512C71.087 14.7424 71.1703 14.7337 71.2492 14.7249V16C71.1265 16.0175 70.9906 16.0351 70.8417 16.0526C70.6927 16.0701 70.5612 16.0789 70.4473 16.0789ZM75.1306 16.1577C74.4032 16.1577 73.7635 15.9781 73.2114 15.6188C72.6681 15.2595 72.2869 14.7775 72.0678 14.1728L73.2114 13.6339C73.4042 14.037 73.6671 14.3568 74.0001 14.5935C74.3419 14.8301 74.7187 14.9484 75.1306 14.9484C75.4812 14.9484 75.766 14.8695 75.9851 14.7118C76.2041 14.554 76.3137 14.3393 76.3137 14.0676C76.3137 13.8924 76.2655 13.7522 76.1691 13.647C76.0727 13.5331 75.95 13.4411 75.801 13.371C75.6608 13.3008 75.5162 13.2483 75.3672 13.2132L74.2499 12.8977C73.6364 12.7225 73.1764 12.4595 72.8696 12.109C72.5717 11.7497 72.4227 11.3334 72.4227 10.8602C72.4227 10.4308 72.5322 10.0584 72.7513 9.74286C72.9704 9.41862 73.2727 9.16886 73.6583 8.99358C74.0439 8.81832 74.4777 8.73068 74.9597 8.73068C75.6082 8.73068 76.1866 8.89281 76.6949 9.21705C77.2032 9.53254 77.5625 9.9751 77.7728 10.5447L76.6292 11.0837C76.489 10.7419 76.2655 10.4702 75.9588 10.2687C75.6608 10.0671 75.3234 9.96633 74.9466 9.96633C74.6223 9.96633 74.3638 10.0452 74.171 10.2029C73.9782 10.3519 73.8818 10.5491 73.8818 10.7945C73.8818 10.961 73.9256 11.1012 74.0133 11.2151C74.1009 11.3203 74.2148 11.4079 74.355 11.478C74.4953 11.5394 74.6399 11.592 74.7888 11.6358L75.9456 11.9776C76.5328 12.1441 76.9841 12.407 77.2996 12.7663C77.6151 13.1168 77.7728 13.5375 77.7728 14.0282C77.7728 14.4489 77.6589 14.8213 77.431 15.1456C77.2119 15.461 76.9052 15.7108 76.5109 15.8948C76.1165 16.0701 75.6564 16.1577 75.1306 16.1577ZM79.6883 16V14.2911H81.2V16H79.6883Z" fill="#645DF6"/>
<path d="M86.7694 16.1577C86.0595 16.1577 85.4286 15.9956 84.8765 15.6714C84.3331 15.3384 83.8993 14.8914 83.5751 14.3306C83.2596 13.7697 83.1019 13.1343 83.1019 12.4245C83.1019 11.7234 83.2596 11.0924 83.5751 10.5316C83.8906 9.97072 84.3244 9.53254 84.8765 9.21705C85.4286 8.89281 86.0595 8.73068 86.7694 8.73068C87.2514 8.73068 87.7027 8.81832 88.1233 8.99358C88.544 9.16009 88.9077 9.39232 89.2144 9.69028C89.5299 9.98824 89.7621 10.3344 89.9111 10.7288L88.6097 11.3334C88.4607 10.9654 88.2197 10.6718 87.8867 10.4527C87.5625 10.2249 87.19 10.1109 86.7694 10.1109C86.3663 10.1109 86.0026 10.2117 85.6783 10.4133C85.3628 10.6061 85.1131 10.8821 84.9291 11.2414C84.745 11.592 84.653 11.9907 84.653 12.4376C84.653 12.8846 84.745 13.2877 84.9291 13.647C85.1131 13.9975 85.3628 14.2736 85.6783 14.4752C86.0026 14.6767 86.3663 14.7775 86.7694 14.7775C87.1988 14.7775 87.5712 14.6679 87.8867 14.4489C88.211 14.221 88.452 13.9187 88.6097 13.5418L89.9111 14.1597C89.7709 14.5365 89.543 14.8783 89.2276 15.185C88.9208 15.483 88.5571 15.7196 88.1365 15.8948C87.7158 16.0701 87.2601 16.1577 86.7694 16.1577ZM91.1658 16V6.04905H92.6512V16H91.1658ZM97.6216 16.1577C96.9381 16.1577 96.3115 15.9956 95.7418 15.6714C95.181 15.3471 94.734 14.9046 94.401 14.3437C94.068 13.7828 93.9015 13.1475 93.9015 12.4376C93.9015 11.719 94.068 11.0837 94.401 10.5316C94.734 9.97072 95.181 9.53254 95.7418 9.21705C96.3027 8.89281 96.9293 8.73068 97.6216 8.73068C98.3227 8.73068 98.9493 8.89281 99.5014 9.21705C100.062 9.53254 100.505 9.97072 100.829 10.5316C101.162 11.0837 101.329 11.719 101.329 12.4376C101.329 13.1562 101.162 13.796 100.829 14.3568C100.496 14.9177 100.049 15.3603 99.4882 15.6845C98.9274 16 98.3052 16.1577 97.6216 16.1577ZM97.6216 14.7775C98.0423 14.7775 98.4147 14.6767 98.739 14.4752C99.0632 14.2736 99.3173 13.9975 99.5014 13.647C99.6942 13.2877 99.7906 12.8846 99.7906 12.4376C99.7906 11.9907 99.6942 11.592 99.5014 11.2414C99.3173 10.8909 99.0632 10.6148 98.739 10.4133C98.4147 10.2117 98.0423 10.1109 97.6216 10.1109C97.2097 10.1109 96.8373 10.2117 96.5043 10.4133C96.18 10.6148 95.9215 10.8909 95.7287 11.2414C95.5447 11.592 95.4526 11.9907 95.4526 12.4376C95.4526 12.8846 95.5447 13.2877 95.7287 13.647C95.9215 13.9975 96.18 14.2736 96.5043 14.4752C96.8373 14.6767 97.2097 14.7775 97.6216 14.7775ZM105.119 16.1577C104.584 16.1577 104.115 16.0394 103.712 15.8028C103.309 15.5574 102.993 15.22 102.766 14.7906C102.547 14.3525 102.437 13.8486 102.437 13.2789V8.88842H103.922V13.1475C103.922 13.4717 103.988 13.7565 104.12 14.0019C104.251 14.2473 104.435 14.4401 104.672 14.5803C104.908 14.7118 105.18 14.7775 105.487 14.7775C105.802 14.7775 106.078 14.7074 106.315 14.5672C106.551 14.427 106.735 14.2298 106.867 13.9756C107.007 13.7215 107.077 13.4235 107.077 13.0818V8.88842H108.55V16H107.143V14.6066L107.301 14.7906C107.134 15.2288 106.858 15.5662 106.473 15.8028C106.087 16.0394 105.636 16.1577 105.119 16.1577ZM113.373 16.1577C112.689 16.1577 112.076 15.9956 111.533 15.6714C110.998 15.3384 110.573 14.8914 110.258 14.3306C109.951 13.7697 109.797 13.1387 109.797 12.4376C109.797 11.7366 109.955 11.1056 110.271 10.5447C110.586 9.98386 111.011 9.5413 111.546 9.21705C112.08 8.89281 112.685 8.73068 113.36 8.73068C113.929 8.73068 114.433 8.84461 114.872 9.07246C115.31 9.30031 115.656 9.61579 115.91 10.0189L115.687 10.3607V6.04905H117.159V16H115.752V14.554L115.923 14.8301C115.678 15.2595 115.327 15.5881 114.872 15.816C114.416 16.0438 113.916 16.1577 113.373 16.1577ZM113.518 14.7775C113.929 14.7775 114.298 14.6767 114.622 14.4752C114.955 14.2736 115.213 13.9975 115.397 13.647C115.59 13.2877 115.687 12.8846 115.687 12.4376C115.687 11.9907 115.59 11.592 115.397 11.2414C115.213 10.8909 114.955 10.6148 114.622 10.4133C114.298 10.2117 113.929 10.1109 113.518 10.1109C113.106 10.1109 112.733 10.2117 112.4 10.4133C112.067 10.6148 111.809 10.8909 111.625 11.2414C111.441 11.592 111.349 11.9907 111.349 12.4376C111.349 12.8846 111.441 13.2877 111.625 13.647C111.809 13.9975 112.063 14.2736 112.387 14.4752C112.72 14.6767 113.097 14.7775 113.518 14.7775Z" fill="#00C2BB"/>
<path d="M15.8331 19.9996V0L0 19.9996H15.8331Z" fill="#00C2BB"/>
<path d="M33.5 8.67844e-05H17.6669V5H29.5417L33.5 8.67844e-05Z" fill="#645DF6"/>
<path d="M17.7501 14.0001V20L22.5 14.0001H17.7501Z" fill="#645DF6"/>
<path d="M28.082 7.00009H17.7905V12H24.1238L28.082 7.00009Z" fill="#00C2BB"/>
</svg>"""

    gap_json_path = os.path.join(JOB_DIR, "projection-audit", "data_gaps.json")
    if not os.path.exists(gap_json_path):
        gap_json_path = os.path.join(JOB_DIR, "data_gaps.json")
    data_gap_html = ""

    # P-2: Query any un-resolved needs_reprice rows
    reprice_rows = []
    try:
        cols = {c[1].lower() for c in conn.execute("PRAGMA table_info('aws_li_to_gcp_li')").fetchall()}
        reprice_cond = "m.strategy = 'needs_reprice'" + (" OR m.needs_reprice = TRUE" if "needs_reprice" in cols else "")
        reprice_rows = conn.execute(f"""
            SELECT c.aws_li_key, c.product, c.usage_type, c.aws_amortized_cost
            FROM aws_li_catalog c
            JOIN aws_li_to_gcp_li m USING (aws_li_key)
            WHERE ({reprice_cond}) AND c.is_workload
        """).fetchall()
    except Exception as _re:
        log.debug(f"Could not query needs_reprice rows: {_re}")

    reprice_total = sum(r[3] or 0.0 for r in reprice_rows) * aws_fx_rate
    reprice_count = len(reprice_rows)

    # Sync into data_gaps.json
    if reprice_count > 0:
        try:
            current_gaps = {"count": 0, "total_cost": 0.0, "items": []}
            if os.path.exists(gap_json_path):
                with open(gap_json_path, encoding="utf-8") as _gf:
                    current_gaps = json.load(_gf)
            # Add needs_reprice section/items if not already present
            existing_keys = {item.get("aws_li_key") for item in current_gaps.get("items", []) if "aws_li_key" in item}
            new_items = []
            for r in reprice_rows:
                if r[0] not in existing_keys:
                    new_items.append({
                        "aws_li_key": r[0],
                        "product": r[1],
                        "usage_type": r[2],
                        "aws_cost": float(r[3] or 0.0),
                        "gap_type": "needs_reprice",
                        "reason": "rate could not be resolved for mappable row — carried at AWS cost",
                    })
            if new_items:
                current_gaps.setdefault("items", []).extend(new_items)
                current_gaps["count"] = len(current_gaps["items"])
                current_gaps["total_cost"] = sum(it.get("aws_cost", 0.0) for it in current_gaps["items"])
                with open(gap_json_path, "w", encoding="utf-8") as _gf:
                    json.dump(current_gaps, _gf, indent=2)
        except Exception as _gwe:
            log.warning(f"Could not update data_gaps.json with needs_reprice: {_gwe}")

    if os.path.exists(gap_json_path):
        try:
            with open(gap_json_path, encoding="utf-8") as _gf:
                _gap_data = json.load(_gf)
            _gc = _gap_data.get("count", 0)
            _gt = _gap_data.get("total_cost", 0.0)
            if _gc > 0 and _gt > 0 and rs_bool("section_data_gaps"):
                data_gap_html = (
                    f'<div style="background:#FEF3C7;border:1px solid #F59E0B;border-radius:8px;'
                    f'padding:16px;margin:20px 0;color:#92400E;">'
                    f'<strong style="font-size:15px">&#9888; Data Gap Warning:</strong> '
                    f'<strong>${_gt:,.2f}</strong> across {_gc} line item(s) could not be classified or fully priced — '
                    f'carried at 1:1 AWS cost parity; needs a rate / review.'
                    f'</div>'
                )
        except Exception as _ge:
            log.warning(f"Could not read data_gaps.json: {_ge}")

    # Explicit banner for needs_reprice if any leftovers exist
    if rs_bool("section_data_gaps") and reprice_count > 0:
        data_gap_html += (
            f'<div style="background:#FFFBEB;border:1px solid #D97706;border-radius:8px;'
            f'padding:16px;margin:20px 0;color:#B45309;">'
            f'<strong style="font-size:15px">&#9888; Rate Resolution Gap:</strong> '
            f'<strong>{curr_symbol}{reprice_total:,.2f}</strong> across {reprice_count} mappable line item(s) '
            f'could not be priced on GCP — carried at AWS cost; needs a rate.'
            f'</div>'
        )


    currency_notice_html = ""
    if curr_code != "USD":
        aws_note = ""
        if aws_fx_rate != 1.0:
            aws_note = f' AWS costs were converted from {currency_info.get("aws_cost_currency", "USD")} to {curr_code} at {aws_fx_rate:g}.'
        currency_notice_html = (
            f'<div style="background:#EFF6FF;border:1px solid #3B82F6;border-radius:8px;'
            f'padding:12px 16px;margin:16px 0;color:#1E40AF;font-size:13px;">'
            f'<strong>&#128177; Currency Notice:</strong> All AWS and GCP costs are shown in <strong>{curr_code}</strong>. '
            f'GCP list prices have been converted from USD ({currency_info.get("label", "")}).'
            f'{aws_note}'
            f'</div>'
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWS → GCP Cost Analysis — {customer_name}</title>
<meta name="description" content="AWS to GCP cloud cost projection for {customer_name}. Prepared by Facets.cloud Cloud Cost Intelligence.">
{CSS}
</head>
<body>

<!-- ── Facets.cloud header ── -->
<header class="fc-header">
  <a href="https://www.facets.cloud/" target="_blank" rel="noopener" class="fc-logo-link">
    {_FC_LOGO_FULL}
  </a>
  <div class="fc-right">
    <div class="fc-badge">AWS → GCP Migration Analysis</div>
    <div class="fc-prepared">Prepared for <b>{_html.escape(customer_name)}</b> &nbsp;·&nbsp; {now.strftime('%B %Y')}</div>
  </div>
</header>
<div class="fc-divider"></div>

<!-- ── report body ── -->
<div class="page-body">

<h1>AWS → GCP Cloud Cost Analysis</h1>
<p class="subhead">
  <b>Customer:</b> {_html.escape(customer_name)} &nbsp;·&nbsp;
  <b>Analysis Date:</b> {now.strftime('%B %Y')} &nbsp;·&nbsp;
  <b>Line Items:</b> {total_li:,} &nbsp;·&nbsp;
  <b>Primary GCP Region:</b> {_html.escape(gcp_region_display)} &nbsp;·&nbsp;
  <b>Currency:</b> {_html.escape(curr_code)}
</p>

{currency_notice_html}
{data_gap_html}
{_row_filter_note}

<div class="summary-grid">
{cards_html}
</div>

<p class="legend">{_verdict_sentence(aws_grand, gcp_grand_od, gcp_grand_1yr, gcp_grand_3yr)}</p>

{capacity_html}

<div class="info-box">
  <b>Pricing assumptions:</b>
  <ul>
    <li>Target region: <b>{_html.escape(gcp_region_display)}</b></li>
    <li>On-Demand list prices — no sustained-use discount applied to base rates</li>
    <li>1-Year and 3-Year CUD columns apply committed-use multipliers per service</li>
    <li>License-included pricing (BYOL not assumed unless the AWS bill indicates it)</li>
    <li>AWS Spot instances → GCP Spot/Preemptible VMs (~60–91% off On-Demand). AWS Spot prices are market-variable and may be lower — GCP may appear higher for Spot-heavy workloads.</li>
    <li>OpenSearch and MSK rows are <b>self-managed infrastructure estimates</b> on GCP, not managed-service equivalents. Treat as directional only.</li>
    <li>Passthrough rows carry the AWS cost as-is — no reliable GCP equivalent found; manual sizing required.</li>
  </ul>
</div>

<h2>Cost Comparison by Service</h2>
<p class="section-meta">Sorted by AWS spend descending. Diff = AWS − GCP On-Demand; <span class="green">green = GCP cheaper</span>, <span class="red">red = GCP more expensive</span>.</p>
<div class="table-scroll"><table id="detail-table">
  <tr>
    {''.join(f'<th class="num">{label}</th>' for _, label, _ in _ACTIVE_COLUMNS)}
    <th class="extra-col">Details</th>
  </tr>
  {detail_rows_html}
</table></div>
<p class="legend">* Diff = AWS − GCP On-Demand. Positive (green) = GCP cheaper; negative (red) = GCP costs more.</p>

{discounts_block_html}

{assumptions_block_html}

<h2>Methodology</h2>
<ul class="method">
  <li>AWS Cost and Usage Report (CUR) line items ingested and classified by billing mechanic (compute, storage, data transfer, managed DB, etc.).</li>
  <li>Deterministic mappings applied first (EBS storage types → Persistent Disk, I/O-only charges → ignore, NAT/TGW/ALB → GCP networking equivalents).</li>
  <li>EC2 and RDS instance families mapped deterministically using instance-family rules — 4th-gen preferred unconditionally regardless of AWS source generation (e.g., c-family → C4/C4D, r/m-family → N4/N4D, t-family (burstable) → cheapest of E2/N4D by real per-region rate — see cost-tier check below, ARM → C4A). Older-gen families are only used when the 4th-gen SKU has no rate at all in that region.</li>
  <li><b>Cost-tier check</b> (rows marked <span class="cost-tier-note" style="font-style:normal">cost-tier</span> in the description): before finalizing a compute row, the tool compares real GCP prices in that row's exact region and swaps to a cheaper option whenever it's the <i>same or better</i> performance guarantee — e.g. a burstable AWS instance can move to a cheaper always-on GCP family (a pure win: AWS's CPU-credit pricing never gets cheaper by staying idle, so this loses nothing and often costs less). It never does the reverse — a sustained-performance AWS instance is never swapped onto a cheaper burstable GCP family, even if that would look cheaper on paper, since that would silently change the performance guarantee you're paying for. Every such swap is noted inline so it's never a silent decision.</li>
  <li>Remaining dynamic rows (misc, managed services) mapped by LLM agents with GCP billing catalog constraints and confidence scoring.</li>
  <li>GCP list prices sourced from the Cloud Billing API catalog (bundled at report generation time). CUD rates are applied from <code>cud_pct.json</code> per service class.</li>
  <li>Outlier safety net: any mapped row with projected GCP/AWS ratio &gt;50× or absolute &gt;$10,000 on &lt;$100 AWS spend is clamped to AWS-cost passthrough.</li>
  <li>AWS Marketplace and Support charges passed through at 1:1 cost (no GCP equivalent).</li>
</ul>

{wins_losses_block}
{coverage_block}
{under_proj_html}

<!-- ── Facets.cloud footer ── -->
<footer class="fc-footer">
  <a href="https://www.facets.cloud/" target="_blank" rel="noopener" class="fc-footer-brand" style="text-decoration:none">
    <svg width="72" height="12" viewBox="0 0 119 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Facets.cloud"><path d="M37.3286 16V6.20679H43.704V7.58704H38.8797V10.5316H43.1782V11.9118H38.8797V16H37.3286ZM46.4592 16.1577C45.9772 16.1577 45.5522 16.0745 45.1841 15.908C44.8248 15.7327 44.5444 15.4961 44.3428 15.1981C44.1412 14.8914 44.0405 14.5321 44.0405 14.1202C44.0405 13.7346 44.1237 13.3885 44.2902 13.0818C44.4655 12.775 44.7328 12.5165 45.0921 12.3062C45.4514 12.0959 45.9027 11.9469 46.446 11.8592L48.9174 11.4517V12.6217L46.7352 13.0029C46.3409 13.073 46.0517 13.2001 45.8677 13.3841C45.6836 13.5594 45.5916 13.7872 45.5916 14.0676C45.5916 14.3393 45.6924 14.5628 45.8939 14.7381C46.1043 14.9046 46.3716 14.9878 46.6958 14.9878C47.0989 14.9878 47.4495 14.9002 47.7474 14.7249C48.0541 14.5496 48.2908 14.3174 48.4573 14.0282C48.6238 13.7303 48.707 13.4016 48.707 13.0423V11.2151C48.707 10.8646 48.5756 10.5798 48.3127 10.3607C48.0585 10.1328 47.7168 10.0189 47.2873 10.0189C46.893 10.0189 46.5468 10.1241 46.2489 10.3344C45.9597 10.536 45.745 10.7989 45.6048 11.1231L44.3691 10.5053C44.5006 10.1547 44.7153 9.84803 45.0132 9.58512C45.3112 9.31345 45.6573 9.10313 46.0517 8.95415C46.4548 8.80517 46.8798 8.73068 47.3268 8.73068C47.8876 8.73068 48.3828 8.83584 48.8122 9.04617C49.2504 9.25649 49.5878 9.55007 49.8244 9.9269C50.0698 10.295 50.1924 10.7244 50.1924 11.2151V16H48.7728V14.7118L49.0751 14.7512C48.9086 15.0404 48.6939 15.2902 48.431 15.5005C48.1768 15.7108 47.8833 15.8729 47.5502 15.9869C47.226 16.1008 46.8623 16.1577 46.4592 16.1577ZM55.1104 16.1577C54.4006 16.1577 53.7696 15.9956 53.2175 15.6714C52.6742 15.3384 52.2404 14.8914 51.9161 14.3306C51.6006 13.7697 51.4429 13.1343 51.4429 12.4245C51.4429 11.7234 51.6006 11.0924 51.9161 10.5316C52.2316 9.97072 52.6654 9.53254 53.2175 9.21705C53.7696 8.89281 54.4006 8.73068 55.1104 8.73068C55.5924 8.73068 56.0437 8.81832 56.4644 8.99358C56.885 9.16009 57.2487 9.39232 57.5554 9.69028C57.8709 9.98824 58.1031 10.3344 58.2521 10.7288L56.9507 11.3334C56.8018 10.9654 56.5608 10.6718 56.2278 10.4527C55.9035 10.2249 55.5311 10.1109 55.1104 10.1109C54.7073 10.1109 54.3436 10.2117 54.0194 10.4133C53.7039 10.6061 53.4541 10.8821 53.2701 11.2414C53.086 11.592 52.994 11.9907 52.994 12.4376C52.994 12.8846 53.086 13.2877 53.2701 13.647C53.4541 13.9975 53.7039 14.2736 54.0194 14.4752C54.3436 14.6767 54.7073 14.7775 55.1104 14.7775C55.5398 14.7775 55.9123 14.6679 56.2278 14.4489C56.552 14.221 56.793 13.9187 56.9507 13.5418L58.2521 14.1597C58.1119 14.5365 57.8841 14.8783 57.5686 15.185C57.2618 15.483 56.8982 15.7196 56.4775 15.8948C56.0569 16.0701 55.6012 16.1577 55.1104 16.1577ZM62.8457 16.1577C62.1358 16.1577 61.5048 15.9956 60.9527 15.6714C60.4094 15.3384 59.9844 14.8914 59.6777 14.3306C59.3709 13.7609 59.2176 13.1256 59.2176 12.4245C59.2176 11.7059 59.3709 11.0705 59.6777 10.5184C59.9931 9.96633 60.4138 9.53254 60.9396 9.21705C61.4654 8.89281 62.0613 8.73068 62.7274 8.73068C63.2619 8.73068 63.7395 8.8227 64.1602 9.00673C64.5808 9.19076 64.9358 9.44491 65.225 9.76915C65.5141 10.0846 65.7332 10.4483 65.8822 10.8602C66.04 11.2721 66.1188 11.7103 66.1188 12.1747C66.1188 12.2887 66.1144 12.407 66.1057 12.5297C66.0969 12.6523 66.0794 12.7663 66.0531 12.8714H60.3875V11.6884H65.2118L64.502 12.2273C64.5896 11.7979 64.5589 11.4167 64.4099 11.0837C64.2697 10.7419 64.0506 10.4746 63.7527 10.2818C63.4635 10.0803 63.1217 9.97948 62.7274 9.97948C62.333 9.97948 61.9825 10.0803 61.6757 10.2818C61.369 10.4746 61.1324 10.755 60.9659 11.1231C60.7994 11.4824 60.7337 11.9206 60.7687 12.4376C60.7249 12.9196 60.7906 13.3403 60.9659 13.6996C61.1499 14.0589 61.4041 14.3393 61.7283 14.5409C62.0613 14.7424 62.4382 14.8432 62.8588 14.8432C63.2882 14.8432 63.6519 14.7468 63.9499 14.554C64.2566 14.3612 64.4976 14.1115 64.6729 13.8047L65.8822 14.3963C65.742 14.7293 65.5229 15.0316 65.225 15.3033C64.9358 15.5662 64.5852 15.7765 64.1733 15.9343C63.7702 16.0833 63.3277 16.1577 62.8457 16.1577ZM70.4473 16.0789C69.7024 16.0789 69.124 15.8685 68.7121 15.4479C68.3003 15.0273 68.0943 14.4357 68.0943 13.6733V10.2292H66.8455V8.88842H67.0427C67.3757 8.88842 67.6342 8.79203 67.8183 8.59923C68.0023 8.40643 68.0943 8.14353 68.0943 7.81051V7.25841H69.5797V8.88842H71.1966V10.2292H69.5797V13.6076C69.5797 13.8529 69.6192 14.0633 69.698 14.2385C69.7769 14.405 69.904 14.5365 70.0792 14.6329C70.2545 14.7205 70.4824 14.7643 70.7628 14.7643C70.8329 14.7643 70.9118 14.76 70.9994 14.7512C71.087 14.7424 71.1703 14.7337 71.2492 14.7249V16C71.1265 16.0175 70.9906 16.0351 70.8417 16.0526C70.6927 16.0701 70.5612 16.0789 70.4473 16.0789ZM75.1306 16.1577C74.4032 16.1577 73.7635 15.9781 73.2114 15.6188C72.6681 15.2595 72.2869 14.7775 72.0678 14.1728L73.2114 13.6339C73.4042 14.037 73.6671 14.3568 74.0001 14.5935C74.3419 14.8301 74.7187 14.9484 75.1306 14.9484C75.4812 14.9484 75.766 14.8695 75.9851 14.7118C76.2041 14.554 76.3137 14.3393 76.3137 14.0676C76.3137 13.8924 76.2655 13.7522 76.1691 13.647C76.0727 13.5331 75.95 13.4411 75.801 13.371C75.6608 13.3008 75.5162 13.2483 75.3672 13.2132L74.2499 12.8977C73.6364 12.7225 73.1764 12.4595 72.8696 12.109C72.5717 11.7497 72.4227 11.3334 72.4227 10.8602C72.4227 10.4308 72.5322 10.0584 72.7513 9.74286C72.9704 9.41862 73.2727 9.16886 73.6583 8.99358C74.0439 8.81832 74.4777 8.73068 74.9597 8.73068C75.6082 8.73068 76.1866 8.89281 76.6949 9.21705C77.2032 9.53254 77.5625 9.9751 77.7728 10.5447L76.6292 11.0837C76.489 10.7419 76.2655 10.4702 75.9588 10.2687C75.6608 10.0671 75.3234 9.96633 74.9466 9.96633C74.6223 9.96633 74.3638 10.0452 74.171 10.2029C73.9782 10.3519 73.8818 10.5491 73.8818 10.7945C73.8818 10.961 73.9256 11.1012 74.0133 11.2151C74.1009 11.3203 74.2148 11.4079 74.355 11.478C74.4953 11.5394 74.6399 11.592 74.7888 11.6358L75.9456 11.9776C76.5328 12.1441 76.9841 12.407 77.2996 12.7663C77.6151 13.1168 77.7728 13.5375 77.7728 14.0282C77.7728 14.4489 77.6589 14.8213 77.431 15.1456C77.2119 15.461 76.9052 15.7108 76.5109 15.8948C76.1165 16.0701 75.6564 16.1577 75.1306 16.1577ZM79.6883 16V14.2911H81.2V16H79.6883Z" fill="#645DF6"/><path d="M86.7694 16.1577C86.0595 16.1577 85.4286 15.9956 84.8765 15.6714C84.3331 15.3384 83.8993 14.8914 83.5751 14.3306C83.2596 13.7697 83.1019 13.1343 83.1019 12.4245C83.1019 11.7234 83.2596 11.0924 83.5751 10.5316C83.8906 9.97072 84.3244 9.53254 84.8765 9.21705C85.4286 8.89281 86.0595 8.73068 86.7694 8.73068C87.2514 8.73068 87.7027 8.81832 88.1233 8.99358C88.544 9.16009 88.9077 9.39232 89.2144 9.69028C89.5299 9.98824 89.7621 10.3344 89.9111 10.7288L88.6097 11.3334C88.4607 10.9654 88.2197 10.6718 87.8867 10.4527C87.5625 10.2249 87.19 10.1109 86.7694 10.1109C86.3663 10.1109 86.0026 10.2117 85.6783 10.4133C85.3628 10.6061 85.1131 10.8821 84.9291 11.2414C84.745 11.592 84.653 11.9907 84.653 12.4376C84.653 12.8846 84.745 13.2877 84.9291 13.647C85.1131 13.9975 85.3628 14.2736 85.6783 14.4752C86.0026 14.6767 86.3663 14.7775 86.7694 14.7775C87.1988 14.7775 87.5712 14.6679 87.8867 14.4489C88.211 14.221 88.452 13.9187 88.6097 13.5418L89.9111 14.1597C89.7709 14.5365 89.543 14.8783 89.2276 15.185C88.9208 15.483 88.5571 15.7196 88.1365 15.8948C87.7158 16.0701 87.2601 16.1577 86.7694 16.1577ZM91.1658 16V6.04905H92.6512V16H91.1658ZM97.6216 16.1577C96.9381 16.1577 96.3115 15.9956 95.7418 15.6714C95.181 15.3471 94.734 14.9046 94.401 14.3437C94.068 13.7828 93.9015 13.1475 93.9015 12.4376C93.9015 11.719 94.068 11.0837 94.401 10.5316C94.734 9.97072 95.181 9.53254 95.7418 9.21705C96.3027 8.89281 96.9293 8.73068 97.6216 8.73068C98.3227 8.73068 98.9493 8.89281 99.5014 9.21705C100.062 9.53254 100.505 9.97072 100.829 10.5316C101.162 11.0837 101.329 11.719 101.329 12.4376C101.329 13.1562 101.162 13.796 100.829 14.3568C100.496 14.9177 100.049 15.3603 99.4882 15.6845C98.9274 16 98.3052 16.1577 97.6216 16.1577ZM97.6216 14.7775C98.0423 14.7775 98.4147 14.6767 98.739 14.4752C99.0632 14.2736 99.3173 13.9975 99.5014 13.647C99.6942 13.2877 99.7906 12.8846 99.7906 12.4376C99.7906 11.9907 99.6942 11.592 99.5014 11.2414C99.3173 10.8909 99.0632 10.6148 98.739 10.4133C98.4147 10.2117 98.0423 10.1109 97.6216 10.1109C97.2097 10.1109 96.8373 10.2117 96.5043 10.4133C96.18 10.6148 95.9215 10.8909 95.7287 11.2414C95.5447 11.592 95.4526 11.9907 95.4526 12.4376C95.4526 12.8846 95.5447 13.2877 95.7287 13.647C95.9215 13.9975 96.18 14.2736 96.5043 14.4752C96.8373 14.6767 97.2097 14.7775 97.6216 14.7775ZM105.119 16.1577C104.584 16.1577 104.115 16.0394 103.712 15.8028C103.309 15.5574 102.993 15.22 102.766 14.7906C102.547 14.3525 102.437 13.8486 102.437 13.2789V8.88842H103.922V13.1475C103.922 13.4717 103.988 13.7565 104.12 14.0019C104.251 14.2473 104.435 14.4401 104.672 14.5803C104.908 14.7118 105.18 14.7775 105.487 14.7775C105.802 14.7775 106.078 14.7074 106.315 14.5672C106.551 14.427 106.735 14.2298 106.867 13.9756C107.007 13.7215 107.077 13.4235 107.077 13.0818V8.88842H108.55V16H107.143V14.6066L107.301 14.7906C107.134 15.2288 106.858 15.5662 106.473 15.8028C106.087 16.0394 105.636 16.1577 105.119 16.1577ZM113.373 16.1577C112.689 16.1577 112.076 15.9956 111.533 15.6714C110.998 15.3384 110.573 14.8914 110.258 14.3306C109.951 13.7697 109.797 13.1387 109.797 12.4376C109.797 11.7366 109.955 11.1056 110.271 10.5447C110.586 9.98386 111.011 9.5413 111.546 9.21705C112.08 8.89281 112.685 8.73068 113.36 8.73068C113.929 8.73068 114.433 8.84461 114.872 9.07246C115.31 9.30031 115.656 9.61579 115.91 10.0189L115.687 10.3607V6.04905H117.159V16H115.752V14.554L115.923 14.8301C115.678 15.2595 115.327 15.5881 114.872 15.816C114.416 16.0438 113.916 16.1577 113.373 16.1577ZM113.518 14.7775C113.929 14.7775 114.298 14.6767 114.622 14.4752C114.955 14.2736 115.213 13.9975 115.397 13.647C115.59 13.2877 115.687 12.8846 115.687 12.4376C115.687 11.9907 115.59 11.592 115.397 11.2414C115.213 10.8909 114.955 10.6148 114.622 10.4133C114.298 10.2117 113.929 10.1109 113.518 10.1109C113.106 10.1109 112.733 10.2117 112.4 10.4133C112.067 10.6148 111.809 10.8909 111.625 11.2414C111.441 11.592 111.349 11.9907 111.349 12.4376C111.349 12.8846 111.441 13.2877 111.625 13.647C111.809 13.9975 112.063 14.2736 112.387 14.4752C112.72 14.6767 113.097 14.7775 113.518 14.7775Z" fill="#00C2BB"/><path d="M15.8331 19.9996V0L0 19.9996H15.8331Z" fill="#00C2BB"/><path d="M33.5 8.67844e-05H17.6669V5H29.5417L33.5 8.67844e-05Z" fill="#645DF6"/><path d="M17.7501 14.0001V20L22.5 14.0001H17.7501Z" fill="#645DF6"/><path d="M28.082 7.00009H17.7905V12H24.1238L28.082 7.00009Z" fill="#00C2BB"/></svg>
    <span>Generated by <b>facets.cloud</b> — Cloud Cost Intelligence Platform</span>
  </a>
  <span class="fc-footer-note">
    Report generated {now.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC &nbsp;·&nbsp;
    engine: {_html.escape(os.environ.get("AGY_MODEL") or _RS.get("default_engine") or "gemini-3.8-flash")} &nbsp;·&nbsp;
    settings v: {_rs_version} &nbsp;·&nbsp; Prices are estimates — verify with GCP Pricing Calculator before committing.
  </span>
</footer>

</div><!-- /.page-body -->
</body>
</html>"""

    # ── write outputs ─────────────────────────────────────────────────────────
    html_path = os.path.join(JOB_DIR, "projection-audit", f"report-{run_id}.html")
    for path in [html_path, os.path.join(JOB_DIR, "projection-audit", "report.html")]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
    log.info(f"Generated {html_path}")

    # ── AI summary markdown (shown in frontend JobStatus card) ────────────────
    summary_md_rel = None
    try:
        baseline = sec_a_aws if sec_a_aws > 0 else (aws_gross if aws_gross > 0 else aws_infra_baseline)
        od_val = sec_a_gcp_od if sec_a_aws > 0 else gcp_od
        c1_val = sec_a_gcp_1yr if sec_a_aws > 0 else gcp_1yr
        c3_val = sec_a_gcp_3yr if sec_a_aws > 0 else gcp_3yr
        od_pct  = (od_val  - baseline) / baseline * 100 if baseline else 0
        c1_pct  = (c1_val - baseline) / baseline * 100 if baseline else 0
        c3_pct  = (c3_val - baseline) / baseline * 100 if baseline else 0
        arrow   = lambda p: ("▲" if p > 0 else "▼") + f" {abs(p):.1f}%"
        verdict = "GCP On-Demand is cost-neutral" if abs(od_pct) < 2 else \
                  f"GCP On-Demand is {arrow(od_pct)} vs AWS Mapped Baseline" + (" — GCP more expensive" if od_pct > 0 else " — GCP cheaper")

        top_saves = "\n".join(
            f"- **{r[0]}** → {r[1]}: save {curr_symbol}{r[4]:,.0f}/mo ({r[4]/r[2]*100:.0f}%)"
            for r in gcp_wins[:3] if r[2]
        ) or "_No significant savings identified._"

        top_costs = "\n".join(
            f"- **{r[0]}** → {r[1]}: +{curr_symbol}{abs(r[4]):,.0f}/mo ({abs(r[4])/r[2]*100:.0f}% more)"
            for r in gcp_loses[:3] if r[2]
        ) or "_No significant cost increases identified._"

        passthrough_rows = conn.execute("""
            SELECT c.product, c.aws_amortized_cost
            FROM aws_li_to_gcp_li m JOIN aws_li_catalog c USING (aws_li_key)
            WHERE m.strategy = 'passthrough' AND c.is_workload AND c.aws_amortized_cost > 0
            GROUP BY c.product, c.aws_amortized_cost
            ORDER BY c.aws_amortized_cost DESC LIMIT 4
        """).fetchall()
        passthrough_note = ", ".join(r[0] for r in passthrough_rows) if passthrough_rows else "none"

        summary_lines = [
            f"## {customer_name} — AWS → GCP Cost Projection",
            "",
            f"**{verdict}**",
            "",
        ]
        if curr_code != "USD":
            summary_lines.extend([
                f"> 💱 **CURRENCY**: All figures shown in **{curr_code}** ({currency_info.get('label', '')}).",
                "",
            ])
        if os.path.exists(gap_json_path):
            try:
                with open(gap_json_path, encoding="utf-8") as _gf:
                    _gap_data = json.load(_gf)
                if _gap_data.get("count", 0) > 0 and _gap_data.get("total_cost", 0) > 0:
                    summary_lines.extend([
                        f"> ⚠️ **DATA GAP WARNING**: {curr_symbol}{_gap_data['total_cost']:,.2f} across {_gap_data['count']} line item(s) could not be classified or fully priced (carried at 1:1 AWS cost).",
                        "",
                    ])
            except Exception:
                pass

        if reprice_count > 0:
            summary_lines.extend([
                f"> ⚠️ **UNRESOLVED RATES (needs_reprice)**: {curr_symbol}{reprice_total:,.2f} across {reprice_count} mappable line item(s) could not be priced on GCP — carried at AWS cost; needs a rate.",
                "",
            ])


        baseline_label = "AWS Mapped Cost (Section A)" if sec_a_aws > 0 else "AWS List Total"
        summary_lines.extend([
            "| Pricing Tier | Monthly Estimate | vs AWS Mapped |",
            "|---|---|---|",
            f"| {baseline_label} | {curr_symbol}{baseline:,.0f} | — |",
            f"| GCP On-Demand (List) | {curr_symbol}{od_val:,.0f} | {arrow(od_pct)} |",
            f"| GCP 1-Year CUD | {curr_symbol}{c1_val:,.0f} | {arrow(c1_pct)} |",
            f"| GCP 3-Year CUD | {curr_symbol}{c3_val:,.0f} | {arrow(c3_pct)} |",
            "",
        ])
        if sec_a_aws > 0 and sec_b_aws > 0:
            summary_lines.extend([
                f"> *Note: Section A covers mapped workloads priced directly to GCP. Carries {curr_symbol}{sec_b_aws:,.0f} 1:1 passthroughs (Section B) in detailed report.*",
                "",
            ])
        if discount_rows:
            disc_md = "\n".join(f"- **{r[0]}**: -{curr_symbol}{r[3]:,.0f}/mo ({r[1] or 'Discount'})" for r in discount_rows[:5])
            summary_lines.extend([
                "### Discounts by Service",
                disc_md,
                f"**Customer Net AWS Total**: {curr_symbol}{aws_grand:,.0f}/mo (List {curr_symbol}{aws_gross:,.0f} − Discounts {curr_symbol}{display_discounts:,.0f})",
                "",
            ])
        summary_lines.extend([
            "### Where GCP saves",
            top_saves,
            "",
            "### Where GCP costs more",
            top_costs,
            "",
            f"### Mapping coverage",
            f"- **{total_li:,}** AWS line items",
            f"- Passthroughs (no GCP equivalent): {passthrough_note}",
            f"- Primary GCP region: {gcp_region_display}",
        ])
        summary_text = "\n".join(summary_lines)
        summary_rel  = os.path.join("projection-audit", f"summary-{run_id}.md")
        summary_abs  = os.path.join(JOB_DIR, summary_rel)
        with open(summary_abs, "w", encoding="utf-8") as sf:
            sf.write(summary_text)
        summary_md_rel = summary_rel
        log.info(f"Generated summary: {summary_abs}")
    except Exception as e:
        log.warning(f"Could not write summary_md: {e}")

    # ── run_results row (for frontend TotalsCard) ─────────────────────────────
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_results (
                run_id TEXT PRIMARY KEY, ts_utc TEXT, run_type TEXT, instruction TEXT,
                aws_total DOUBLE, gcp_od DOUBLE, gcp_1yr_cud DOUBLE, gcp_3yr_cud DOUBLE,
                report_html TEXT, report_md TEXT, summary_md TEXT,
                mapped_rows INTEGER, passthroughs INTEGER, confidence TEXT
            )
        """)
        if not conn.execute("SELECT 1 FROM run_results WHERE run_id=?", [run_id]).fetchone():
            mapped_rows  = conn.execute("SELECT COUNT(*) FROM aws_li_to_gcp_li").fetchone()[0] or 0
            passthroughs = conn.execute("SELECT COUNT(*) FROM aws_li_to_gcp_li WHERE strategy='passthrough'").fetchone()[0] or 0
            final_aws_total = sec_a_aws if sec_a_aws > 0 else aws_infra_baseline
            final_gcp_od = sec_a_gcp_od if sec_a_aws > 0 else gcp_od
            final_gcp_1yr = sec_a_gcp_1yr if sec_a_aws > 0 else gcp_1yr
            final_gcp_3yr = sec_a_gcp_3yr if sec_a_aws > 0 else gcp_3yr
            conn.execute(
                "INSERT INTO run_results "
                "(run_id,ts_utc,run_type,instruction,aws_total,gcp_od,gcp_1yr_cud,gcp_3yr_cud,"
                " report_html,report_md,summary_md,mapped_rows,passthroughs,confidence) "
                "VALUES (?,?,?,NULL,?,?,?,?,'projection-audit/report.html',NULL,?,?,?,NULL)",
                # aws_total stores Section A workload baseline matching report headline card.
                # gcp_od/1yr/3yr are Section A GCP pricing matching report headline cards.
                [run_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"), "initial",
                 final_aws_total, final_gcp_od, final_gcp_1yr, final_gcp_3yr, summary_md_rel, mapped_rows, passthroughs]
            )
            conn.commit()
            log.info(f"Inserted run_results row for {run_id}")
    except Exception as e:
        log.warning(f"Could not write run_results row: {e}")

    conn.close()


if __name__ == "__main__":
    main()

