#!/usr/bin/env python3
import json
import os
import sys
import re
import duckdb

# Add scripts directory to path to import resolve_sku
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from apply_static_mappings import resolve_sku, cheapest_in_scope, cheapest_gpu_in_scope, _no_rate_suffix, _family_hourly_rate, _gp_family_by_label, _strict_resolve_sku, _GP_FAMILY_GENERATION

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "projection-audit/projection.duckdb"
MANIFEST_PATH = "projection-audit/phase2_manifest.json"
MAPPINGS_DIR = "projection-audit/mappings"

_ITYPE_RE = re.compile(r'^(?:db\.)?([a-z]+)(\d+)([a-z]*)\.(.*)$')

# Fallback instance-type extraction from usage_type when the instance_type
# column itself is NULL — confirmed real: one bill's ingest left
# instance_type NULL for all 100 rows despite usage_type carrying it plainly
# ("APS3-BoxUsage:m6a.16xlarge"). Without this, main() below drops every
# compute_breakdown/managed_db row entirely (parse_instance(None) -> None),
# silently routing them all to the LLM instead of the deterministic mappers
# (and, for GPU rows specifically, bypassing _aws_gpu_spec()'s VRAM/class
# floor entirely, since that also depends on a real instance_type string).
# Mirrors the same "prefer instance_type column, fall back to usage_type
# regex" resilience apply_static_mappings.py's map_compute_burstable/
# map_compute_arm/map_msk already have — compute_breakdown/managed_db were
# the only paths still missing it.
_ITYPE_FROM_UT_RE = re.compile(
    # Also matches abbreviated size suffixes like "2xl" (as in "db.m5.2xl" from
    # some billing formats that truncate "2xlarge" to "2xl"). The "l" alternative
    # only fires at a word boundary, so "2xlarge" still matches via "large" first.
    r'(?:BoxUsage:|InstanceUsage:)?\b((?:db\.)?[a-z][a-z0-9]*\.[0-9]*x?(?:large|medium|small|micro|nano|metal|l))\b',
    re.IGNORECASE,
)


def _resolve_instance_type(r):
    """Return a usable instance-type string for this row: the instance_type
    column if present, else a best-effort extraction from usage_type, else
    from operation. Confirmed real: an EDP/private-rate-card discounted bill
    left usage_type blank entirely, with the instance type only appearing in
    operation ("([EC2 PRC] EC2 Discount @ 35.00%) $0.676 per On Demand Linux
    r5.4xlarge Instance (247.258 Hrs)") — checking usage_type alone silently
    dropped every such row back to the generic small-instance fallback spec."""
    itype = r.get("instance_type")
    if itype:
        return itype
    m = _ITYPE_FROM_UT_RE.search(r.get("usage_type") or "")
    if m:
        itype = m.group(1)
        # Expand abbreviated size suffix: "db.m5.2xl" → "db.m5.2xlarge"
        if itype.endswith("xl") and not itype.endswith("xlarge"):
            itype += "arge"
        return itype
    m = _ITYPE_FROM_UT_RE.search(r.get("operation") or "")
    if m:
        itype = m.group(1)
        if itype.endswith("xl") and not itype.endswith("xlarge"):
            itype += "arge"
        return itype
    return None


def _backfill_specs_from_ec2_types(r, itype):
    """When instance_vcpus/instance_ram_gb are missing (same root cause as a
    missing instance_type — this ingest simply never populated them), derive
    the real spec from the bundled ec2-instance-types.json instead of letting
    map_gce_row/map_db_row's own `or 2`/`or 8.0` fallback silently substitute
    a generic small-instance default for what might be a 64-vCPU/256GB
    instance. Only fills in what's actually missing; never overrides a real
    value already present."""
    if r.get("instance_vcpus") and r.get("instance_ram_gb"):
        return
    # ec2-instance-types.json keys are plain EC2 style ("m6a.16xlarge") with
    # no "db." prefix, even for RDS instance types.
    key = (itype or "").lower().strip()
    if key.startswith("db."):
        key = key[3:]
    entry = _EC2_TYPES.get(key)
    if not entry:
        return
    if not r.get("instance_vcpus") and entry.get("vcpus"):
        r["instance_vcpus"] = entry["vcpus"]
    if not r.get("instance_ram_gb") and entry.get("ram_gb"):
        r["instance_ram_gb"] = entry["ram_gb"]

# Load semantic family map config
MAP_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "family_map.json")
with open(MAP_CONFIG_PATH) as f:
    _cfg = json.load(f)

_AWS_WORKLOAD = _cfg["aws_workload"]   # prefix → workload type
_ARCH_SUFFIX  = _cfg["arch_suffix"]    # suffix char → architecture
_GCP_FAMILIES = _cfg["gcp_families"]   # workload → arch → {family, arm_sku, min_aws_gen, ...}
_ARM_PREFIX   = set(_cfg.get("arm_prefix", []))  # prefixes that are always ARM (e.g. a1 Graviton)

# Real per-AWS-instance-type GPU model/VRAM/count — the AWS-side requirement
# for cheapest_gpu_in_scope()'s floor, replacing gpu_profiles' old
# generation-number-only guess (which mapped every pre-gen-4 "p" instance to
# A100 regardless of whether the real hardware was K80/V100/A100).
EC2_TYPES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ec2-instance-types.json")
with open(EC2_TYPES_PATH) as f:
    _EC2_TYPES = json.load(f)

_ACCEL_RE = re.compile(r'^(\d+)x\s+NVIDIA\s+(?:Tesla\s+)?([A-Za-z0-9]+)(?:\s+(\d+)GB)?$', re.IGNORECASE)


def _aws_gpu_spec(instance_type):
    """Return (model, vram_gb, count) parsed from ec2-instance-types.json's
    accelerator field for this real AWS instance type, or None if this
    instance type isn't in the catalog or has no accelerator (K80-based
    types like p2.* aren't sold as real GCE GPU SKUs any more — see
    _discover_gpu_models — so they fall through to the caller's own
    generation-based fallback rather than a hard failure)."""
    entry = _EC2_TYPES.get(instance_type)
    if not entry:
        return None
    accel = (entry.get("accelerator") or "").strip()
    m = _ACCEL_RE.match(accel)
    if not m:
        return None
    count = int(m.group(1))
    model = m.group(2).upper()
    vram = int(m.group(3)) if m.group(3) else None
    return model, vram, count


def parse_instance(itype):
    if not itype:
        return None
    m = _ITYPE_RE.match(itype.lower())
    if not m:
        return None
    return {
        "prefix": m.group(1),
        "gen": int(m.group(2)),
        "suffixes": m.group(3),
        "size": m.group(4)
    }


def get_gcp_family(parsed):
    """Return (gcp_family, arm_sku) or (None, False).

    Semantic lookup: AWS prefix → workload type, suffix → architecture,
    then find the matching GCP family from config. gen-based fallback is
    also data-driven via min_aws_gen / prev_gen_family in family_map.json.
    """
    prefix   = parsed["prefix"]
    suffixes = parsed["suffixes"]
    gen      = parsed["gen"]

    workload = _AWS_WORKLOAD.get(prefix, "general")

    # Prefix-based ARM families (e.g. a1 Graviton) take priority over suffix detection.
    # arm_prefix in family_map.json lists prefixes that are intrinsically ARM.
    if prefix in _ARM_PREFIX:
        arch = "arm"
    else:
        # Detect architecture from suffix characters (e.g. 'g'→arm, 'a'→amd)
        arch = "intel"
        for char, detected in _ARCH_SUFFIX.items():
            if char in suffixes:
                arch = detected
                break

    target = _GCP_FAMILIES.get(workload, {}).get(arch)
    if not target:
        return None, False

    family = target["family"]
    if gen < target.get("min_aws_gen", 1):
        family = target.get("prev_gen_family", family)

    return family, target.get("arm_sku", False)

# Deterministic OS-license lookup for operating systems not already handled by a
# dedicated static mapper. Windows EC2 rows are classified into a separate
# "compute_windows" mechanic group (see classify_mechanics.py) and priced by
# map_compute_windows() in apply_static_mappings.py — they never reach here.
# RHEL has no dedicated classifier/mapper, so its rows land in the generic
# compute_breakdown path (this file) — _license_entries() below adds the
# missing license component here instead, keyed off operating_system, so the
# AWS "License Included" premium (bundled into the instance rate) isn't lost
# when GCP prices core+RAM only.
#
# RHEL license fee is a flat per-VM-hour charge banded by vCPU count (Red Hat's
# per-VM subscription model), not a CPU+RAM split like Windows.
_RHEL_VCPU_BANDS = [
    (127, "Red Hat Enterprise Linux 9 on VM with 128 VCPU or more"),
    (8, "Red Hat Enterprise Linux 9 on VM with 9 to 127 VCPU"),
    (0, "Red Hat Enterprise Linux 9 on VM with up to 8 VCPU"),
]


def _license_entries(r, aws_key, gcp_region, vcpus, ram):
    """Return a list of license mapping entries (0 or 1), or [] if the OS
    carries no separate GCP license charge (e.g. plain Linux, or BYOL)."""
    os_name = (r.get("operating_system") or "").strip().lower()
    license_model = (r.get("license_model") or "").strip().lower()
    if not os_name or "byol" in license_model:
        return []

    if os_name == "rhel" or "red hat" in os_name:
        desc = next(d for min_v, d in _RHEL_VCPU_BANDS if vcpus > min_v)
        sku = resolve_sku("Compute Engine", desc, gcp_region)
        return [{
            "aws_li_key": aws_key, "gcp_service": "Compute Engine",
            "gcp_sku_name": desc, "component": "license",
            "strategy": "map" if sku else "passthrough", "unit_multiplier": 1.0, "gcp_region": gcp_region,
            "projection_note": f"Deterministic OS-license mapping: {desc}" + _no_rate_suffix(sku, gcp_region),
            "mapping_confidence": 0.85, "is_workload": True, "break_down": True,
            **({"gcp_sku_id": sku, "gcp_sku_unit": sku.unit} if sku else {}),
        }]

    return []


def map_gce_row(r, parsed):
    gcp_family, arm_sku = get_gcp_family(parsed)
    if not gcp_family:
        return None

    gcp_region = r.get("gcp_region")
    aws_key = r["aws_li_key"]
    vcpus = r.get("instance_vcpus") or 2
    ram = r.get("instance_ram_gb") or 8.0

    # burstable workload: E2/T2A have no burst-credit model — projection assumes
    # steady-state CPU usage, which may overstate cost for mostly-idle workloads.
    is_burstable = _AWS_WORKLOAD.get(parsed["prefix"]) == "burstable"

    # arm_sku comes from family_map.json — ARM GCP families use "Arm" in catalog SKU names.
    arm_infix = "Arm " if arm_sku else ""

    # Same-tier cost comparison: family_map.json's config assigns ONE fixed
    # family per workload/architecture (e.g. every burstable row -> E2, every
    # AMD general-purpose row -> N2D AMD) regardless of region. This was the
    # single highest-traffic mapper still missing the check every other
    # compute mapper now has — it handles the bulk of ordinary EC2 fleet
    # (m5/c5/r5/m6i/etc.), not just the special cases (t-family/Windows/ARM/
    # MSK). `cheapest_in_scope()` sweeps every x86 family in `_GP_FAMILIES`
    # tagged for this row's tier AND workload — burstable sources may match
    # against burstable+sustained (a pure win, never a downgrade), sustained
    # sources stay within sustained only. ARM rows are handled separately
    # above (arm_sku branch) and never enter this x86 sweep.
    #
    # workload scoping: a compute-optimized AWS source (c4/c5/c6i/etc.) must
    # only compare against GCP's own compute-optimized line (C2D AMD/C3/C3D/
    # C4/C4D/C4N) — never cross into general-purpose N2/N4/N4D even if
    # cheaper, same Performance-Tier Safety Rule logic as burstable-vs-
    # sustained. Confirmed a real gap here: family_map.json's pre-gen-6
    # fallback for "compute" workload pointed straight to "N2" (general-
    # purpose) — silently crossing tiers for c4/c5 (~48.6% under this due to
    # N2's higher rate, not because N2 is genuinely equivalent). AWS's
    # "memory" workload stays in the "general" registry pool deliberately —
    # GCP's dedicated M-series memory-optimized family has large fixed
    # minimum sizes unsuited to typical r5.large-style requests, so
    # custom-ratio N-series remains the practical same-tier match there.
    #
    # "storage" workload (AWS i3/d2/h1, local-NVMe-emphasis instances) DOES
    # get its own pool — Z3, GCP's storage-optimized family, discovered
    # dynamically from the catalog (see _discover_gp_families in
    # apply_static_mappings.py). Unlike M3/M4, Z3's catalog rate is a real
    # per-vCPU/per-GB linear rate (same billing shape as N2/N4/etc, not a
    # fixed-bundle price), so it's mathematically usable at arbitrary
    # requested sizes — this was a confirmed real gap, not a deliberate
    # exclusion: storage-optimized sources never had ANY same-tier storage-
    # optimized GCP candidate to compare against before, only the
    # general-purpose pool. Z3's real-world minimum provisioning size isn't
    # captured by the linear catalog rate, so a switch to Z3 gets an explicit
    # verify-with-customer caveat rather than being trusted silently.
    workload_type = _AWS_WORKLOAD.get(parsed["prefix"], "general")
    if workload_type == "compute":
        workloads = ("compute",)
    elif workload_type == "storage":
        workloads = ("storage", "general")
    else:
        workloads = ("general",)

    # EFA/HPC-class AWS instance families are marketed specifically for
    # tightly-coupled distributed workloads (HPC, distributed training) that
    # depend on high-bandwidth/low-latency networking, not just vCPU/RAM —
    # a real, previously-missed axis (confirmed via direct grep: nothing in
    # this pipeline tracked network tier at all before this fix). This is an
    # AWS instance-family identity fact, not something derivable from usage
    # data, so it's a small explicit list, same class as the tier/workload
    # tables above.
    _EFA_HPC_FAMILIES = {"hpc6a", "hpc6id", "hpc7a", "hpc7g", "c5n", "m5n", "m5dn", "r5n", "r5dn"}
    aws_family_key = (r.get("instance_type") or "").split(".")[0].lower()
    min_network_tier = "high" if aws_family_key in _EFA_HPC_FAMILIES else "standard"

    cost_tag = ""
    if not arm_sku:
        default_label = gcp_family
        tiers = ("burstable", "sustained") if is_burstable else ("sustained",)
        # Burstable rows (default_label="E2", gen 2) must floor any cost-tier
        # switch against N4D's generation (4), not E2's own — same fix as
        # apply_static_mappings.py's map_compute_burstable/map_msk, applied
        # here too since this is the rare safety-net path a burstable EC2 row
        # takes when it isn't caught upstream by classify_mechanics.py first.
        # Without it, N2D AMD/T2D AMD (also gen 2) cleared the floor and won
        # on price alone — the legacy-hardware substitution the floor exists
        # to prevent.
        min_gen = _GP_FAMILY_GENERATION.get("N4D", 4) if is_burstable else None
        switched_family, core_desc, ram_desc, switched, reason = cheapest_in_scope(
            default_label, vcpus, ram, gcp_region, archs=("x86",), tiers=tiers, workloads=workloads,
            min_network_tier=min_network_tier, min_generation=min_gen)
        if switched:
            gcp_family = switched_family
            cost_tag = (f" [cost-tier: {switched_family} cheaper here, same/better tier]" if reason == "cheaper"
                        else f" [cost-tier: {default_label} unavailable in region — {switched_family} used instead, same/better tier]")
            if switched_family == "Z3":
                cost_tag += (" [architecture review recommended: Z3 is GCP's storage-optimized "
                             "family, genuinely cheaper here for this vCPU/RAM shape, but its "
                             "real-world minimum provisioning size isn't captured by the linear "
                             "per-vCPU rate used for this comparison — verify with customer that "
                             "a Z3 instance is actually offered at this size before finalizing]")

        # Symmetric counterpart to the ARM-branch's x86 disclosure below: an
        # x86 burstable AWS source (t3/t3a/etc.) never had the reverse
        # direction checked at all — the x86 sweep above only ever considers
        # other x86 families, never ARM (T2A), even though a burstable
        # source may legally cross to any cheaper option including an
        # architecture change (disclosed, not silently switched — ARM vs x86
        # is a real binary-compatibility boundary, same reasoning as the
        # other direction). Only checked for burstable sources: a sustained
        # x86 source crossing to ARM would be a genuine recompile requirement
        # with no guaranteed performance parity, a bigger ask than for a
        # burstable source already accepting variable performance.
        if is_burstable:
            t2a_entry = _gp_family_by_label("T2A Arm")
            if t2a_entry:
                _, t2a_core, t2a_ram, _, _, _, _ = t2a_entry
                t2a_core_rate = _family_hourly_rate("Compute Engine", t2a_core, gcp_region)
                t2a_ram_rate = _family_hourly_rate("Compute Engine", t2a_ram, gcp_region)
                x86_core_rate = _family_hourly_rate("Compute Engine", core_desc, gcp_region)
                x86_ram_rate = _family_hourly_rate("Compute Engine", ram_desc, gcp_region)
                if (t2a_core_rate is not None and t2a_ram_rate is not None
                        and x86_core_rate is not None and x86_ram_rate is not None):
                    t2a_total = vcpus * t2a_core_rate + ram * t2a_ram_rate
                    x86_total = vcpus * x86_core_rate + ram * x86_ram_rate
                    if t2a_total < x86_total:
                        pct = round((1 - t2a_total / x86_total) * 100)
                        cost_tag += (f" [architecture review recommended: ARM T2A would be ~{pct}% cheaper "
                                     f"here for this vCPU/RAM shape than {gcp_family} — not switched "
                                     "automatically since x86 binaries aren't ARM-compatible without a "
                                     "rebuild; confirm with customer whether ARM is viable]")
    else:
        # Same-tier cost/availability sweep as the x86 branch above, scoped to
        # archs=("arm",) only — an ARM AWS source may fall back to a cheaper
        # or more-available ARM family (e.g. C4A Arm -> N4A), but NEVER
        # crosses to x86 automatically (see the disclosure-only logic below).
        # Previously this branch had no fallback at all: if family_map.json's
        # single default ARM family (C4A Arm) had no rate in this region, the
        # row went straight to passthrough even when a real, cheaper/available
        # ARM alternative (N4A) existed for the exact same vCPU/RAM shape —
        # confirmed real gap (the Delhi/asia-south2 case: C4A Arm has no SKU
        # there at all, and N4A does — but the old code never looked past the
        # single hardcoded default to find it). `reason='unavailable'` here
        # means the default had no rate at all in this region (not a cost
        # optimization) — `reason='cheaper'` means both were priced and the
        # alternative genuinely undercut the default.
        arm_default_label = f"{gcp_family} {arm_infix.strip()}".strip()
        arm_tiers = ("burstable", "sustained") if is_burstable else ("sustained",)
        switched_family, core_desc, ram_desc, switched, reason = cheapest_in_scope(
            arm_default_label, vcpus, ram, gcp_region, archs=("arm",), tiers=arm_tiers,
            workloads=workloads, min_network_tier=min_network_tier)
        if switched:
            gcp_family = switched_family
            cost_tag = (f" [cost-tier: {switched_family} cheaper here, same/better tier]" if reason == "cheaper"
                        else f" [cost-tier: {arm_default_label} unavailable in region — {switched_family} used "
                             "instead, same/better tier]")

        # ARM sources never silently cross to an x86 GCP family — unlike
        # burstable-vs-sustained (same architecture, only a performance/billing
        # model difference, always a safe crossing per CLAUDE.md §8), ARM vs
        # x86 is a genuine instruction-set/binary-compatibility boundary a
        # customer running compiled Graviton binaries can't just ignore. But a
        # burstable ARM source (t4g/t4g-class) CAN be disclosed a cheaper x86
        # burstable option exists, same architecture-review pattern already
        # used for Z3/GPU-alias switches — never switched automatically, only
        # surfaced so the customer can decide whether x86 is viable for them.
        if is_burstable:
            e2_core_rate = _family_hourly_rate("Compute Engine", "E2 Instance Core", gcp_region)
            e2_ram_rate = _family_hourly_rate("Compute Engine", "E2 Instance Ram", gcp_region)
            # core_desc/ram_desc already come straight out of the sweep above —
            # they name whichever ARM family actually got picked (default or
            # fallback), so no need to re-derive the label/lookup it again.
            if e2_core_rate is not None and e2_ram_rate is not None:
                arm_core_rate = _family_hourly_rate("Compute Engine", core_desc, gcp_region)
                arm_ram_rate = _family_hourly_rate("Compute Engine", ram_desc, gcp_region)
                if arm_core_rate is not None and arm_ram_rate is not None:
                    arm_total = vcpus * arm_core_rate + ram * arm_ram_rate
                    e2_total = vcpus * e2_core_rate + ram * e2_ram_rate
                    if e2_total < arm_total:
                        pct = round((1 - e2_total / arm_total) * 100)
                        cost_tag += (f" [architecture review recommended: x86 E2 would be ~{pct}% cheaper "
                                     f"here for this vCPU/RAM shape than {gcp_family} — not switched "
                                     "automatically since Graviton/ARM binaries aren't x86-compatible "
                                     "without a rebuild; confirm with customer whether x86 is viable]")

    # burstable workload note: computed AFTER the family is finalized (above)
    # so it names whichever family was actually picked, not a hardcoded "E2" —
    # a burstable source may have switched to N2D/T2D/etc. via cheapest_in_scope.
    burst_note = (f" [Note: {gcp_family} has no burst-credit model; projection assumes steady-state CPU usage]"
                  if is_burstable else "")

    # 1. Core Component
    # core_desc/ram_desc came out of cheapest_in_scope() above (both the x86
    # and ARM branches), which decides availability with STRICT exact-region
    # matching — plain resolve_sku() would go through lookup_sku_in_catalog()'s
    # continent fallback instead, silently substituting a different region's
    # SKU/price for a family cheapest_in_scope() already determined has no
    # rate here at all (confirmed real: an ARM row in Delhi resolving to
    # Taiwan's C4A Arm price). _strict_resolve_sku() keeps the two in sync.
    core_sku = _strict_resolve_sku("Compute Engine", core_desc, gcp_region)
    core_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Compute Engine",
        "gcp_sku_name": core_desc,
        "component": "core",
        "strategy": "map" if core_sku else "passthrough",
        "unit_multiplier": float(vcpus),
        "gcp_region": gcp_region,
        "projection_note": f"Deterministic mapping: GCE {gcp_family} Core{burst_note}{cost_tag}" + _no_rate_suffix(core_sku, gcp_region),
        "mapping_confidence": 0.85 if is_burstable else 1.0,
        "is_workload": True,
        "break_down": True
    }
    if core_sku:
        core_entry["gcp_sku_id"] = core_sku
        core_entry["gcp_sku_unit"] = core_sku.unit

    # 2. RAM Component
    ram_sku = _strict_resolve_sku("Compute Engine", ram_desc, gcp_region)
    ram_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Compute Engine",
        "gcp_sku_name": ram_desc,
        "component": "ram",
        "strategy": "map" if ram_sku else "passthrough",
        "unit_multiplier": float(ram),
        "gcp_region": gcp_region,
        "projection_note": f"Deterministic mapping: GCE {gcp_family} RAM{burst_note}{cost_tag}" + _no_rate_suffix(ram_sku, gcp_region),
        "mapping_confidence": 0.85 if is_burstable else 1.0,
        "is_workload": True,
        "break_down": True
    }
    if ram_sku:
        ram_entry["gcp_sku_id"] = ram_sku
        ram_entry["gcp_sku_unit"] = ram_sku.unit
        
    mappings = [core_entry, ram_entry]
    
    # 3. Local SSD Suffix Handling or Storage Optimized
    if "d" in parsed["suffixes"] or parsed["prefix"] in ("i", "im", "is", "d", "h"):
        # "Local SSD Capacity" never matched any real catalog SKU (confirmed
        # via find-sku.sh) — resolve_sku always returned None here, silently
        # degrading every local-SSD-backed row (i3/i3en/d2/d3/h1/*d-suffix
        # families) to strategy='passthrough'. Local SSD is priced per
        # GCP FAMILY, not a single flat rate (e.g. "Z3 Instance Local SSD" =
        # $0.08/GiBy.mo vs "C4 Instance Local SSD" = $0.16/GiBy.mo in the same
        # region) — use the row's own resolved gcp_family, matching the
        # core/ram desc pattern shape. Falls back to the older, family-generic
        # "SSD backed Local Storage" naming (used by N-series/E2-class
        # families that predate the newer per-family SKU convention) if the
        # family-specific pattern isn't found.
        ssd_desc = f"{gcp_family} Instance Local SSD"
        ssd_sku = resolve_sku("Compute Engine", ssd_desc, gcp_region)
        if not ssd_sku:
            ssd_desc = "SSD backed Local Storage"
            ssd_sku = resolve_sku("Compute Engine", ssd_desc, gcp_region)
        # Local SSD is billed per GiB-MONTH ("GiBy.mo"), but projection_view.py's
        # formula is total_usage(hours) * unit_multiplier * rate — correct for
        # core/ram (rate is per vCPU/GiB-HOUR) but not for this component, whose
        # rate is monthly. Pre-dividing by avg hours/month here converts the
        # hours-based usage quantity into an effective month-fraction before the
        # shared hourly formula multiplies it, instead of multiplying 375 GB by
        # ~730 hours directly against a monthly rate (a ~730x/26,784-vs-$36.69
        # inflation observed on m6gd.4xlarge — confirmed exact match).
        _AVG_HOURS_PER_MONTH = 730.0
        ssd_entry = {
            "aws_li_key": aws_key,
            "gcp_service": "Compute Engine",
            "gcp_sku_name": ssd_desc,
            "component": "storage",
            "strategy": "map" if ssd_sku else "passthrough",
            "unit_multiplier": 375.0 / _AVG_HOURS_PER_MONTH,  # 1 SSD increment (375 GB), hours->month-corrected
            "gcp_region": gcp_region,
            "projection_note": "Local SSD suffix / storage-optimized VM attachment (GiB-month rate, hours->month corrected)" + _no_rate_suffix(ssd_sku, gcp_region),
            "mapping_confidence": 0.95,
            "is_workload": True,
            "break_down": True
        }
        if ssd_sku:
            ssd_entry["gcp_sku_id"] = ssd_sku
            ssd_entry["gcp_sku_unit"] = ssd_sku.unit
        mappings.append(ssd_entry)

    mappings.extend(_license_entries(r, aws_key, gcp_region, vcpus, ram))

    return mappings

def _resolve_gpu_count(entry, vcpus):
    """Compute GPU count from a gpu_profiles config entry and the instance vCPU count."""
    if "fixed_count" in entry:
        return float(entry["fixed_count"])
    if "gpu_per_vcpu" in entry:
        return max(float(entry.get("min_count", 1)), vcpus * entry["gpu_per_vcpu"])
    if "vcpu_breakpoints" in entry:
        for bp in sorted(entry["vcpu_breakpoints"], key=lambda x: -x["min_vcpu"]):
            if vcpus >= bp["min_vcpu"]:
                return float(bp["count"])
        return float(entry.get("default_count", 1))
    return 1.0


def _gpu_profile(prefix, gen, vcpus):
    """Return (gcp_family, gpu_desc, gpu_count) from gpu_profiles config, or None.

    Kept only as the last-resort fallback for _aws_gpu_spec() misses (an AWS
    instance type not present in ec2-instance-types.json, or an accelerator
    string that doesn't parse) — the primary path is now the real AWS
    model/VRAM/count from _aws_gpu_spec() swept through cheapest_gpu_in_scope(),
    not this generation-number guess."""
    for entry in _cfg.get("gpu_profiles", {}).get(prefix, []):
        min_gen = entry.get("min_gen", 1)
        max_gen = entry.get("max_gen", 9999)
        if min_gen <= gen <= max_gen:
            return entry["family"], entry["gpu_desc"], _resolve_gpu_count(entry, vcpus)
    return None


# AWS-branded accelerators with no real GCE-attachable equivalent in the
# catalog (confirmed via _discover_gpu_models's live scan): K80 (GCE dropped
# general availability years ago — only sold via managed AI Platform/Vertex
# "Batch Prediction" SKUs today, a different product); Habana Gaudi, Xilinx
# FPGA, AMD Radeon Pro (no GCP equivalent hardware at all); H200 (only sold
# bundled into the A3Ultra Autopilot machine shape, not as a separate
# attachable line item the way T4/L4/A100/H100 are — doesn't fit this
# core+ram+accelerator split, needs its own architecture, out of scope here).
# These fall through to passthrough (same honest "no fair equivalent" pattern
# as OpenSearch/CloudFront) rather than forcing a wrong mapping.
_AWS_GPU_NO_EQUIVALENT = {"K80", "GAUDI", "HL-205", "VU9P", "V520", "H200"}
# Raw accelerator-string markers for non-Nvidia hardware. Needed because
# _aws_gpu_spec()'s parser only recognizes "NVIDIA ..." accelerator strings —
# an AMD/Habana/Xilinx accelerator never reaches _AWS_GPU_NO_EQUIVALENT at all
# (it fails to parse), and previously fell through to the OLD generation-based
# gpu_profiles table, which blindly assumes any AWS prefix/gen combination it
# recognizes means Nvidia hardware — confirmed real bug: g4ad.xlarge (AMD
# Radeon Pro V520) was silently labeled "Nvidia Tesla T4 GPU" at 0.95
# confidence, because g4ad's prefix "g"+gen 4 coincidentally matches g4dn's
# real T4 bucket in that table. Checking the raw accelerator text directly
# (not just the parsed model) closes that gap for every non-Nvidia case, not
# just the ones _aws_gpu_spec happens to tokenize.
_NON_NVIDIA_ACCEL_RE = re.compile(r"\b(AMD|Habana|Xilinx|Gaudi|FPGA|Radeon)\b", re.IGNORECASE)
# Cross-vendor/cross-family aliases to the nearest real GCP-discoverable model —
# a genuine capability judgment call (both modern ~24GB inference-class cards),
# not a catalog fact, so it's a small manual table like everything else here.
_AWS_GPU_MODEL_ALIAS = {"A10G": "L4", "T4G": "T4"}


def _no_gpu_equivalent_entry(r, aws_key, gcp_region, vcpus, ram, why):
    """Honest passthrough for an AWS accelerator with no real GCP equivalent —
    same pattern as OpenSearch/CloudFront's "no fair equivalent" entries.
    Returns core+ram entries only (billed as plain compute, no accelerator
    line) since there's no GCP SKU to attach a fabricated GPU cost to."""
    note = f"No GCP GPU equivalent for this AWS accelerator ({why}) — priced as plain compute only, accelerator cost not represented"
    entries = []
    for component, unit_mult in (("core", vcpus), ("ram", ram)):
        entries.append({
            "aws_li_key": aws_key,
            "gcp_service": "Compute Engine",
            "gcp_sku_name": None,
            "component": component,
            "strategy": "passthrough",
            "unit_multiplier": float(unit_mult),
            "gcp_region": gcp_region,
            "projection_note": note,
            "mapping_confidence": 0.40,
            "is_workload": True,
            "break_down": True,
        })
    return entries


def map_gpu_row(r, parsed):
    prefix = parsed["prefix"]
    gen = parsed["gen"]
    gcp_region = r.get("gcp_region")
    aws_key = r["aws_li_key"]
    vcpus = r.get("instance_vcpus") or 8
    ram = r.get("instance_ram_gb") or 32.0

    itype = r.get("instance_type") or ""
    accel_raw = ""
    from apply_static_mappings import _GPU_VRAM_GB_FALLBACK  # noqa: reused below too
    entry = _EC2_TYPES.get(itype)
    if entry:
        accel_raw = (entry.get("accelerator") or "")
    # AWS sells Graviton+GPU instances (g5g.*: ARM64 host CPU + NVIDIA T4G) —
    # confirmed real via ec2-instance-types.json's "arch" field. Every GPU
    # candidate this pipeline discovers (cheapest_gpu_in_scope, G2/A2/A3-class)
    # is x86-hosted — there's no GCP ARM-hosted-GPU offering to match natively.
    # This is the same real architecture-compatibility crossing disclosed
    # everywhere else in this codebase (never silently swapped, only
    # surfaced) — previously undisclosed here specifically for the GPU row's
    # own host-CPU component, even though the GPU MODEL substitution
    # (T4G→T4 via _AWS_GPU_MODEL_ALIAS) was already disclosed.
    is_arm_host = bool(entry) and (entry.get("arch") or "").lower() == "arm64"

    if _NON_NVIDIA_ACCEL_RE.search(accel_raw):
        return _no_gpu_equivalent_entry(r, aws_key, gcp_region, vcpus, ram, accel_raw.strip())

    spec = _aws_gpu_spec(itype)
    cost_tag = ""
    if spec and spec[0] in _AWS_GPU_NO_EQUIVALENT:
        return _no_gpu_equivalent_entry(r, aws_key, gcp_region, vcpus, ram, f"{spec[2]}x {spec[0]}")

    alias_note = ""
    if spec:
        model, vram, gpu_count = spec
        aliased_model = _AWS_GPU_MODEL_ALIAS.get(model, model)
        if aliased_model != model:
            alias_note = (f" [no exact GCP equivalent for Nvidia {model}; substituted {aliased_model} "
                          "— different generation/performance tier, verify with customer]")
        model = aliased_model
        if vram is None:
            vram = _GPU_VRAM_GB_FALLBACK.get(model)
        if vram is not None:
            best_model, best_vram, core_desc, ram_desc, gpu_desc, gpu_display_desc, switched, reason = \
                cheapest_gpu_in_scope(model, vram, gpu_count, vcpus, ram, gcp_region)
            if switched:
                cost_tag = (f" [cost-tier: {best_model} {best_vram}GB cheaper here, same/better "
                             "class — verify with customer that this GPU model/size is actually "
                             "available at this size before finalizing]" if reason == "cheaper"
                             else f" [cost-tier: {model} {vram}GB unavailable in region — "
                             f"{best_model} {best_vram}GB used instead, same/better class — "
                             "verify with customer]")
        else:
            core_desc = ram_desc = gpu_desc = gpu_display_desc = None
    else:
        core_desc = ram_desc = gpu_desc = gpu_display_desc = None

    if core_desc is None:
        # Genuinely no signal at all (instance type not in ec2-instance-types.json,
        # or an unrecognized accelerator string format) — fall back to the old
        # generation-based guess as a last resort, NOT for any case we can
        # positively identify as "no GCP equivalent" (those are handled above).
        profile = _gpu_profile(prefix, gen, vcpus)
        if not profile:
            return None
        gcp_family, gpu_desc, gpu_count = profile
        gpu_display_desc = gpu_desc
        core_desc = f"{gcp_family} Instance Core"
        ram_desc = f"{gcp_family} Instance Ram"

    host_arch_note = (" [no ARM-hosted GPU offering on GCP for this accelerator class — "
                      "host CPU silently would have been x86 either way, but flagging since the "
                      "AWS source runs Graviton/ARM64; verify application compatibility with "
                      "customer]") if is_arm_host else ""

    # Core
    # Same reasoning as map_gce_row() above: core_desc/gpu_desc came out of
    # cheapest_gpu_in_scope()'s strict-region availability sweep — use the
    # strict resolver so a family/model it already ruled out here can't come
    # back via resolve_sku()'s continent fallback.
    core_sku = _strict_resolve_sku("Compute Engine", core_desc, gcp_region)
    core_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Compute Engine",
        "gcp_sku_name": core_desc,
        "component": "core",
        "strategy": "map" if core_sku else "passthrough",
        "unit_multiplier": float(vcpus),
        "gcp_region": gcp_region,
        "projection_note": f"GPU workload mapping: GCE {core_desc.replace(' Instance Core', '')} Core" + cost_tag + host_arch_note + _no_rate_suffix(core_sku, gcp_region),
        "mapping_confidence": 0.75 if is_arm_host else 0.95,
        "is_workload": True,
        "break_down": True
    }
    if core_sku:
        core_entry["gcp_sku_id"] = core_sku
        core_entry["gcp_sku_unit"] = core_sku.unit

    # RAM
    ram_sku = _strict_resolve_sku("Compute Engine", ram_desc, gcp_region)
    ram_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Compute Engine",
        "gcp_sku_name": ram_desc,
        "component": "ram",
        "strategy": "map" if ram_sku else "passthrough",
        "unit_multiplier": float(ram),
        "gcp_region": gcp_region,
        "projection_note": f"GPU workload mapping: GCE {ram_desc.replace(' Instance Ram', '')} RAM" + _no_rate_suffix(ram_sku, gcp_region),
        "mapping_confidence": 0.95,
        "is_workload": True,
        "break_down": True
    }
    if ram_sku:
        ram_entry["gcp_sku_id"] = ram_sku
        ram_entry["gcp_sku_unit"] = ram_sku.unit

    # GPU
    gpu_sku = _strict_resolve_sku("Compute Engine", gpu_desc, gcp_region)
    gpu_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Compute Engine",
        "gcp_sku_name": gpu_display_desc,
        "component": "accelerator",
        "strategy": "map" if gpu_sku else "passthrough",
        "unit_multiplier": gpu_count,
        "gcp_region": gcp_region,
        "projection_note": f"Nvidia Accelerator attachment ({gpu_display_desc})" + alias_note + cost_tag + _no_rate_suffix(gpu_sku, gcp_region),
        "mapping_confidence": 0.75 if alias_note else 0.95,
        "is_workload": True,
        "break_down": True
    }
    if gpu_sku:
        gpu_entry["gcp_sku_id"] = gpu_sku
        gpu_entry["gcp_sku_unit"] = gpu_sku.unit
        
    return [core_entry, ram_entry, gpu_entry]

def map_db_row(r, parsed):
    gcp_family, arm_sku = get_gcp_family(parsed)
    if not gcp_family:
        return None
    # Cloud SQL genuinely has no ARM machine type at all (confirmed against the
    # real catalog — no "Cloud SQL for X: ... Arm ..." SKU exists anywhere), so
    # there's no wrong-architecture SKU choice to make here the way compute has
    # (there's nothing to silently or correctly pick between). Still disclose
    # it for a Graviton-based RDS source (db.t4g/db.r6g/db.m6g/etc.) — the
    # same "is this architecture crossing OK" question a customer running
    # ARM-specific tooling/extensions might reasonably have, same as every
    # other ARM-crossing disclosure in this codebase, just with a definitive
    # answer instead of a cost comparison (there is no alternative to weigh).
    arm_source_note = (" [source is a Graviton/ARM64 RDS instance — Cloud SQL has no ARM "
                       "machine type at all, so x86 is the only option regardless of cost; "
                       "no compatibility concern for the DB engine itself, but note for any "
                       "ARM-specific client tooling]") if arm_sku else ""
        
    gcp_region = r.get("gcp_region")
    aws_key = r["aws_li_key"]
    vcpus = r.get("instance_vcpus") or 2
    ram = r.get("instance_ram_gb") or 8.0
    is_ha = r.get("deployment_option") == "Multi-AZ" or "multi-az" in (r.get("operation") or "").lower()
    
    suffix = " (Regional)" if is_ha else ""

    # GCP catalog uses "Cloud SQL for MySQL: Zonal - vCPU in <region>" format.
    # The "- vCPU" / "- RAM" substring (with dash) is required for lookup_sku_in_catalog
    # to find an exact match; "Zonal vCPU" (no dash) returns None.
    #
    # MySQL and PostgreSQL are both real, separately-named Cloud SQL SKUs with
    # IDENTICAL vCPU/RAM rates (confirmed against the bundled catalog: same
    # $/vCPU and $/GB in every region checked) — but this function used to
    # hardcode "MySQL" in the display name for EVERY engine regardless of the
    # AWS source, so a PostgreSQL customer's report showed "Cloud SQL for
    # MySQL" as their target. The dollar figure was never wrong (rates match),
    # but the SKU LABEL was factually incorrect — a customer skimming the
    # report would see the wrong engine. Branch on the real database_engine
    # instead of hardcoding one.
    #
    # MariaDB has NO native Cloud SQL equivalent at all — Cloud SQL only
    # offers MySQL/PostgreSQL/SQL Server. MySQL is the closest (MariaDB is a
    # MySQL fork, largely wire-compatible), but this is a genuine engine
    # substitution, not a like-for-like rename — must be flagged honestly,
    # not silently priced as if it were the same product.
    engine = (r.get("database_engine") or "").strip().lower()
    engine_note = ""
    engine_confidence = 0.90
    if "postgres" in engine:
        cloud_sql_engine = "PostgreSQL"
        # Cloud SQL is correctly the automatic mapping (a real infrastructure
        # equivalent, per this project's evidence-vs-intent philosophy —
        # AlloyDB requires workload-context CUR alone can't establish, e.g.
        # analytics/read-scaling needs). But CLAUDE.md §5 explicitly calls
        # AlloyDB out as a real architectural alternative for PostgreSQL
        # sources specifically (Cloud SQL has no equivalent for MySQL/other
        # engines) — mention it as a disclosed option to evaluate, never an
        # auto-mapping, same as the OpenSearch "Possible Alternatives" pattern
        # this project's own design doc already specifies but this pipeline
        # had never actually surfaced anywhere for RDS/PostgreSQL rows.
        engine_note = (" [possible architectural alternative: AlloyDB for PostgreSQL — a fully "
                       "PostgreSQL-compatible managed service with better read-scaling/analytics "
                       "performance than Cloud SQL, at a real price premium; requires workload "
                       "context this bill alone doesn't provide, so not auto-mapped — worth "
                       "evaluating with customer if analytics/scaling was a driver for this instance]")
    elif "mariadb" in engine:
        cloud_sql_engine = "MySQL"
        engine_note = (" [engine substitution: Cloud SQL has no native MariaDB offering — "
                       "MySQL used as the closest available equivalent (MariaDB is a MySQL "
                       "fork, largely wire-compatible but not identical); verify application "
                       "compatibility with customer before migrating]")
    elif "mysql" in engine:
        cloud_sql_engine = "MySQL"
    else:
        # Engine genuinely undetermined (blank database_engine, e.g. a flat
        # Cost-Explorer export with no engine field/text anywhere in the row) —
        # MySQL here is a passthrough DEFAULT, not a detected engine, and must
        # be disclosed as such. Silently presenting it as if MySQL were
        # confirmed is the same undisclosed-wrong-target failure mode
        # CLAUDE.md §7 documents for Aurora ACU rows, just a different code
        # path; the storage-row mapper (apply_static_mappings.py) already
        # avoids this by falling back to a generic "Cloud SQL: {tier}" with no
        # engine claimed when detection fails — mirror that honesty here too.
        cloud_sql_engine = "MySQL"
        engine_note = (" [engine could not be determined from CUR — MySQL assumed as a "
                       "default, not a detected engine; verify actual database engine with "
                       "customer before finalizing]")
        engine_confidence = 0.55

    tier = "Regional" if is_ha else "Zonal"
    core_desc = f"Cloud SQL for {cloud_sql_engine}: {tier} - vCPU"
    ram_desc = f"Cloud SQL for {cloud_sql_engine}: {tier} - RAM"

    # 1. Core Component
    core_sku = resolve_sku("Cloud SQL", core_desc, gcp_region)
    core_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Cloud SQL",
        "gcp_sku_name": core_desc,
        "component": "core",
        "strategy": "map" if core_sku else "passthrough",
        "unit_multiplier": float(vcpus),
        "gcp_region": gcp_region,
        "projection_note": f"Deterministic DB mapping: Cloud SQL for {cloud_sql_engine} {tier} vCPU ({gcp_family} family equivalent)" + engine_note + arm_source_note + _no_rate_suffix(core_sku, gcp_region),
        "mapping_confidence": engine_confidence,
        "is_workload": True,
        "break_down": True
    }
    if core_sku:
        core_entry["gcp_sku_id"] = core_sku
        core_entry["gcp_sku_unit"] = core_sku.unit

    # 2. RAM Component
    ram_sku = resolve_sku("Cloud SQL", ram_desc, gcp_region)
    ram_entry = {
        "aws_li_key": aws_key,
        "gcp_service": "Cloud SQL",
        "gcp_sku_name": ram_desc,
        "component": "ram",
        "strategy": "map" if ram_sku else "passthrough",
        "unit_multiplier": float(ram),
        "gcp_region": gcp_region,
        "projection_note": f"Deterministic DB mapping: Cloud SQL for {cloud_sql_engine} {tier} RAM ({gcp_family} family equivalent)" + engine_note + arm_source_note + _no_rate_suffix(ram_sku, gcp_region),
        "mapping_confidence": engine_confidence,
        "is_workload": True,
        "break_down": True
    }
    if ram_sku:
        ram_entry["gcp_sku_id"] = ram_sku
        ram_entry["gcp_sku_unit"] = ram_sku.unit
        
    return [core_entry, ram_entry]

def main():
    if not os.path.exists(MANIFEST_PATH):
        print("phase2_manifest.json not found; skipping family_mapper")
        sys.exit(0)
        
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
        
    compute_mapped = []
    db_mapped = []
    
    compute_skipped_keys = set()
    db_skipped_keys = set()
    
    unknowns = []
    
    # Map GCE Compute Instances
    for r in manifest.get("compute_breakdown", {}).get("rows", []):
        itype = _resolve_instance_type(r)
        if itype and not r.get("instance_type"):
            # Mutate so downstream map_gce_row/map_gpu_row (which read
            # r["instance_type"] directly, e.g. _aws_gpu_spec()) see the
            # same fallback-resolved value, not the original None.
            r["instance_type"] = itype
        if itype:
            _backfill_specs_from_ec2_types(r, itype)
        parsed = parse_instance(itype)
        if not parsed:
            continue
            
        is_gpu = _AWS_WORKLOAD.get(parsed["prefix"]) == "accelerated"
        if is_gpu:
            res = map_gpu_row(r, parsed)
        else:
            res = map_gce_row(r, parsed)
            
        if res:
            compute_mapped.extend(res)
            compute_skipped_keys.add(r["aws_li_key"])
        else:
            unknowns.append(f"compute: {itype}")

    # Map RDS Database Instances
    for r in manifest.get("managed_db", {}).get("rows", []):
        itype = _resolve_instance_type(r)
        if itype and not r.get("instance_type"):
            r["instance_type"] = itype
        if itype:
            _backfill_specs_from_ec2_types(r, itype)
        parsed = parse_instance(itype)
        if not parsed:
            continue
            
        res = map_db_row(r, parsed)
        if res:
            db_mapped.extend(res)
            db_skipped_keys.add(r["aws_li_key"])
        else:
            unknowns.append(f"database: {itype}")
            
    # Write mapping files if any mapped
    os.makedirs(MAPPINGS_DIR, exist_ok=True)
    if compute_mapped:
        with open(os.path.join(MAPPINGS_DIR, "compute_breakdown_fm_mappings.json"), "w") as f:
            json.dump(compute_mapped, f, indent=2)
        print(f"  family_mapper: mapped {len(compute_skipped_keys)} compute row(s) deterministically")

    if db_mapped:
        with open(os.path.join(MAPPINGS_DIR, "managed_db_fm_mappings.json"), "w") as f:
            json.dump(db_mapped, f, indent=2)
        print(f"  family_mapper: mapped {len(db_skipped_keys)} database row(s) deterministically")

    # Prune phase2_manifest.json to avoid double mapping by LLM
    pruned = False
    if compute_skipped_keys:
        manifest["compute_breakdown"]["rows"] = [
            r for r in manifest["compute_breakdown"]["rows"]
            if r["aws_li_key"] not in compute_skipped_keys
        ]
        manifest["compute_breakdown"]["row_count"] = len(manifest["compute_breakdown"]["rows"])
        pruned = True
        
    if db_skipped_keys:
        manifest["managed_db"]["rows"] = [
            r for r in manifest["managed_db"]["rows"]
            if r["aws_li_key"] not in db_skipped_keys
        ]
        manifest["managed_db"]["row_count"] = len(manifest["managed_db"]["rows"])
        pruned = True
        
    if pruned:
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print("  family_mapper: pruned manifest.json")
        
    # Log unknown families for coverage reviews
    if unknowns:
        log_dir = os.path.dirname(DB_PATH)
        with open(os.path.join(log_dir, "unknown_families.log"), "w") as f:
            f.write("\n".join(unknowns) + "\n")
        print(f"  family_mapper: logged {len(unknowns)} unknown instance type(s)")

if __name__ == "__main__":
    main()
