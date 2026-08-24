"""
candidate_pool.py

Phase D candidate generation + scoring engine.

Replaces the old "AWS family -> single hardcoded GCP target" approach with:
  1. Candidate pool generation (multiple compatible GCP families per workload profile,
     not one fixed answer)
  2. Hard constraint filtering (vCPU/RAM floor, architecture, local NVMe, network tier,
     GPU model+VRAM+count)
  3. Weighted scoring of surviving candidates (capacity fit, generation recency,
     exact-match bonus)
  4. A full accept/reject trace per candidate for the Phase 6 explainability output
  5. mapping_confidence, kept separate from the classification_confidence produced
     upstream in Phase B (catalog-lookup confidence) — this number reflects how good
     the chosen GCP target actually is, not how sure we are what the AWS SKU is.

This module is catalog-agnostic: load_candidate_families()/load_gpu_candidates() read
from the placeholder YAML files in this directory. Swap those two functions for live
queries against your existing catalog.duckdb GCP catalog when wiring this into the
real pipeline — the rest of the engine (constraints, scoring, recommend()) doesn't
need to change.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

NETWORK_RANK = {"standard": 0, "high": 1, "efa": 2}

# GCE predefined vCPU steps used when sizing ratio-based families. Real catalog
# data should replace this with the actual per-family valid size list.
_VALID_VCPU_STEPS = [2, 4, 8, 16, 32, 48, 64, 96, 128, 176, 208]

# Absolute path to the data directory — used by resolve_compute_family so it
# works regardless of the caller's working directory.
_DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "data",
)

_BUILTIN_CANDIDATE_FAMILIES = {
    # x86_64 general/compute-optimized (≤4 GB/vCPU): N2D AMD.
    # Best price-performance for c/m-family workloads on GCP. ~30% cheaper than N2.
    "N2D": {
        "family": "n2d",
        "architecture": "x86_64",
        "min_vcpu": 2,
        "max_vcpu": 224,
        "ram_per_vcpu": 4,
        "local_nvme": False,
        "network_tier": "high",
    },
    # x86_64 memory-optimized (8 GB/vCPU): N2 Intel.
    # Used for AWS r5/r6i/r6a/r7i/r7a families. N2D only offers 4 GB/vCPU —
    # sizing an r5.2xlarge (64 GB) as N2D would double vCPUs to 16 and inflate
    # cost. N2 standard is 8 GB/vCPU and available in all major GCP regions.
    "N2": {
        "family": "n2",
        "architecture": "x86_64",
        "min_vcpu": 2,
        "max_vcpu": 128,
        "ram_per_vcpu": 8,
        "local_nvme": False,
        "network_tier": "high",
    },
    # x86_64 ultra-memory (16 GB/vCPU): M3 Intel.
    # Used for AWS x1e/x2i families (16 GB/vCPU). Available in major regions
    # including asia-south1 (Mumbai). M1/M2 are legacy; M3 is current gen.
    "M3": {
        "family": "m3",
        "architecture": "x86_64",
        "min_vcpu": 4,
        "max_vcpu": 128,
        "ram_per_vcpu": 16,
        "local_nvme": False,
        "network_tier": "high",
    },
    # x86_64 burstable/cost-optimized: E2.
    # AWS t2/t3/t3a equivalent. Capped at 32 vCPU; standard network.
    "E2": {
        "family": "e2",
        "architecture": "x86_64",
        "min_vcpu": 2,
        "max_vcpu": 32,
        "ram_per_vcpu": 8,
        "local_nvme": False,
        "network_tier": "standard",
        "burstable": True,
    },
    # arm64 (Graviton): C4A Axion — preferred over T2A.
    # Available in 28 regions including asia-south1 (Mumbai). T2A is only 5
    # regions. C4A is the GCP-intended Graviton analogue; standard shape is
    # 4 GB/vCPU (c4a-standard), highmem is 8 GB/vCPU (c4a-highmem).
    "C4A": {
        "family": "c4a",
        "architecture": "arm64",
        "min_vcpu": 1,
        "max_vcpu": 72,
        "ram_per_vcpu": 4,
        "local_nvme": False,
        "network_tier": "high",
    },
    # arm64 highmem (8 GB/vCPU): C4A highmem — for Graviton r-family (r6g/r7g).
    "C4A_HIGHMEM": {
        "family": "c4a",
        "architecture": "arm64",
        "min_vcpu": 1,
        "max_vcpu": 72,
        "ram_per_vcpu": 8,
        "local_nvme": False,
        "network_tier": "high",
    },
    # arm64 fallback for regions where C4A is unavailable (5 regions only).
    "T2A": {
        "family": "t2a",
        "architecture": "arm64",
        "min_vcpu": 1,
        "max_vcpu": 48,
        "ram_per_vcpu": 4,
        "local_nvme": False,
        "network_tier": "standard",
    },
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    family: str
    architecture: str
    generation: int
    local_nvme: bool = False
    network_tier: str = "standard"

    # Ratio-based families (vCPU chosen at sizing time, memory derived from ratio)
    gb_per_vcpu: Optional[float] = None
    vcpu_min: Optional[int] = None
    vcpu_max: Optional[int] = None

    # Fixed-size families (burstable shapes, GPU node shapes)
    fixed_vcpu: Optional[int] = None
    fixed_memory_gb: Optional[float] = None

    # GPU-only fields
    gpu_model: Optional[str] = None
    gpu_vram_gb: Optional[float] = None
    gpu_count: Optional[int] = None
    exact_gpu_match: bool = True


@dataclass
class CheckResult:
    passed: bool
    expected: Optional[Any] = None
    actual: Optional[Any] = None
    note: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"passed": self.passed}
        if self.expected is not None: d["expected"] = self.expected
        if self.actual   is not None: d["actual"]   = self.actual
        if self.note     is not None: d["note"]      = self.note
        return d


@dataclass
class ScoredCandidate:
    candidate: Candidate
    accepted: bool
    sized_vcpu: Optional[int] = None
    sized_memory_gb: Optional[float] = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    # Per-check pass/fail: {"cpu": True, "ram": True, "architecture": True,
    # "storage": False, "network": True}. Only keys actually evaluated for this
    # candidate are present — a non-GPU row won't have a "gpu_count" key,
    # and storage/network are only present when the profile requires them.
    # This lets the UI say "rejected because local_nvme=FAIL, all others PASS"
    # without recomputing anything at render time.
    checks: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_candidate_families(path: str = "candidate_families.yaml") -> dict:
    """
    Loads the candidate families catalog from a YAML file.

    Tries the given path first (for backward compatibility when the caller passes
    an explicit path), then tries the canonical data directory path
    (.../data/candidate_families.yaml). Falls back to a minimal built-in dict
    covering the three core families (N2D, E2, T2A) so the module is functional
    without any YAML file present.
    """
    canonical = os.path.join(_DATA_DIR, "candidate_families.yaml")
    for candidate_path in [path, canonical]:
        try:
            with open(candidate_path) as f:
                return yaml.safe_load(f)
        except (FileNotFoundError, OSError):
            continue
    # Fall back to built-in minimal catalog
    return _BUILTIN_CANDIDATE_FAMILIES


def load_gpu_candidates(path: str = "gpu_candidates.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _candidates_from_yaml(entries: list[dict]) -> list[Candidate]:
    out = []
    for e in entries:
        out.append(Candidate(
            family=e["family"],
            architecture=e.get("architecture", "x86_64"),
            generation=e.get("generation", 1),
            local_nvme=e.get("local_nvme", False),
            network_tier=e.get("max_network_tier", "standard"),
            gb_per_vcpu=e.get("gb_per_vcpu"),
            vcpu_min=e.get("vcpu_min"),
            vcpu_max=e.get("vcpu_max"),
            fixed_vcpu=e.get("fixed_vcpu"),
            fixed_memory_gb=e.get("fixed_memory_gb"),
            gpu_model=e.get("gpu_model"),
            gpu_vram_gb=e.get("gpu_vram_gb"),
            gpu_count=e.get("gpu_count"),
            exact_gpu_match=e.get("exact_match", True),
        ))
    return out


# ---------------------------------------------------------------------------
# Pool generation
# ---------------------------------------------------------------------------

def get_candidate_pool(profile: dict, families_catalog: dict, gpu_catalog: dict) -> list[Candidate]:
    """
    Builds the raw (pre-filter) candidate pool for a workload_profile.

    GPU rows pull from gpu_candidates.yaml keyed on (gpu_model, gpu_vram_gb) and
    don't blend with non-GPU pools. Non-GPU rows pull from candidate_families.yaml
    using primary_strategy plus any secondary_strategies, so e.g. a storage-optimized
    memory instance (r5dn) still considers storage-flavored families, not just
    memory-flavored ones.
    """
    service = profile["service"]

    if profile.get("gpu"):
        model = profile["gpu_model"]
        vram = str(int(profile["gpu_vram_gb"]))
        entries = (
            gpu_catalog.get("gpu_models", {})
                       .get(model, {})
                       .get(vram, {})
                       .get("candidates", [])
        )
        return _candidates_from_yaml(entries)

    pool: list[Candidate] = []
    seen = set()
    strategies = [profile["primary_strategy"]] + list(profile.get("secondary_strategies", []))
    for strategy in strategies:
        entries = families_catalog.get(service, {}).get(strategy, {}).get("candidates", [])
        for c in _candidates_from_yaml(entries):
            if c.family not in seen:
                pool.append(c)
                seen.add(c.family)
    return pool


# ---------------------------------------------------------------------------
# Sizing (ratio-based families need a concrete vCPU/memory point before scoring)
# ---------------------------------------------------------------------------

def _round_up_to_valid_size(vcpu: float) -> int:
    for v in _VALID_VCPU_STEPS:
        if v >= vcpu:
            return v
    return _VALID_VCPU_STEPS[-1]


def _capacity_check(req_vcpu: float, req_mem: float, c: Candidate):
    """
    Returns (cpu_ok, ram_ok, sized_vcpu, sized_memory_gb). cpu_ok/ram_ok are
    independent — a candidate can fail on RAM alone, which the old single
    bool _size_candidate() couldn't express. Sizing values are None when the
    family can't be sized to meet the requirement at all (e.g. out of range).
    """
    if c.fixed_vcpu is not None:
        cpu_ok = c.fixed_vcpu >= req_vcpu
        ram_ok = c.fixed_memory_gb >= req_mem
        if cpu_ok and ram_ok:
            return True, True, c.fixed_vcpu, c.fixed_memory_gb
        return cpu_ok, ram_ok, None, None

    if not c.gb_per_vcpu:
        return False, False, None, None

    needed_vcpu = max(req_vcpu, req_mem / c.gb_per_vcpu)
    sized_vcpu = _round_up_to_valid_size(needed_vcpu)
    if c.vcpu_min and sized_vcpu < c.vcpu_min:
        sized_vcpu = c.vcpu_min
    in_range = not (c.vcpu_max and sized_vcpu > c.vcpu_max)
    if not in_range:
        return False, False, None, None

    sized_mem = round(sized_vcpu * c.gb_per_vcpu, 1)
    return sized_vcpu >= req_vcpu, sized_mem >= req_mem, sized_vcpu, sized_mem


# ---------------------------------------------------------------------------
# Hard constraints
# ---------------------------------------------------------------------------

def apply_hard_constraints(profile: dict, pool: list[Candidate]) -> list[ScoredCandidate]:
    """
    Filters the raw pool to compatible candidates. Every check is evaluated for
    every candidate — nothing short-circuits on the first failure — and the full
    pass/fail dict is stored on `checks`, not just whichever reason was found
    first. A candidate that's both the wrong architecture AND missing local NVMe
    shows both, so the UI (or a re-run) never has to recompute anything to explain
    a rejection.
    """
    results: list[ScoredCandidate] = []
    req_vcpu = profile["vcpu"]
    req_mem = profile["memory_gb"]
    req_arch = profile["architecture"]
    req_validations = set(profile.get("required_validations", []))
    req_network = profile.get("network_tier", "standard")
    is_gpu_row = profile.get("gpu", False)

    for c in pool:
        checks: dict[str, bool] = {}

        checks["architecture"] = (c.architecture == req_arch)

        if is_gpu_row:
            checks["gpu_count"] = c.gpu_count is not None and c.gpu_count >= profile.get("gpu_count", 1)
            if checks["gpu_count"]:
                cpu_ok, ram_ok = True, True
                sized_vcpu, sized_mem = c.fixed_vcpu, c.fixed_memory_gb
            else:
                cpu_ok, ram_ok, sized_vcpu, sized_mem = False, False, None, None
        else:
            cpu_ok, ram_ok, sized_vcpu, sized_mem = _capacity_check(req_vcpu, req_mem, c)

        checks["cpu"] = cpu_ok
        checks["ram"] = ram_ok

        if "local_nvme" in req_validations:
            checks["storage"] = c.local_nvme
        if "network" in req_validations:
            checks["network"] = NETWORK_RANK.get(c.network_tier, 0) >= NETWORK_RANK.get(req_network, 0)

        accepted = all(checks.values())

        reasons = []
        if accepted and is_gpu_row and not c.exact_gpu_match:
            reasons.append("nearest-fit GPU substitute, not an exact model match")
        if accepted:
            reasons.append("passed all required checks")

        results.append(ScoredCandidate(
            c, accepted=accepted,
            sized_vcpu=sized_vcpu if accepted else None,
            sized_memory_gb=sized_mem if accepted else None,
            checks=checks, reasons=reasons,
        ))

    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_candidates(profile: dict, scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """
    Scores accepted candidates only. mapping_confidence = this score for the
    winning candidate. Weighted composite:
      - capacity_fit (50%):       1.0 if sized exactly to the requirement, decays
                                   as the candidate over-provisions vCPU/RAM
      - generation_recency (30%): prefers newer GCP generations
      - exact_match_bonus (20%):  full marks unless this is a nearest-fit GPU
                                   substitute with no true hardware equivalent
    """
    req_vcpu = profile["vcpu"]
    req_mem = profile["memory_gb"]

    for sc in scored:
        if not sc.accepted:
            continue
        c = sc.candidate

        vcpu_fit = req_vcpu / sc.sized_vcpu if sc.sized_vcpu else 0
        mem_fit = req_mem / sc.sized_memory_gb if sc.sized_memory_gb else 0
        capacity_fit = (vcpu_fit + mem_fit) / 2

        generation_score = min(c.generation / 6, 1.0)
        exact_bonus = 1.0 if c.exact_gpu_match else 0.7

        score = round((0.5 * capacity_fit) + (0.3 * generation_score) + (0.2 * exact_bonus), 3)
        sc.score = score
        sc.reasons.append(
            f"capacity_fit={capacity_fit:.2f} generation={generation_score:.2f} "
            f"exact_match={exact_bonus:.2f} -> score={score}"
        )

    return scored


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def recommend(profile: dict, families_catalog: dict, gpu_catalog: dict) -> dict:
    """
    Full Phase D pipeline for one CUR row: pool -> hard constraints -> score -> rank.
    Returns a dict ready to attach to the row for Phase 6 reporting, including the
    full candidate trace (accepted + rejected, with reasons) for explainability.
    """
    pool = get_candidate_pool(profile, families_catalog, gpu_catalog)

    if not pool:
        return {
            "instance_type": profile["instance_type"],
            "recommendation": None,
            "mapping_confidence": 0.0,
            "classification_confidence": profile.get("classification_confidence"),
            "status": "no_candidates_found",
            "note": "Route to outlier flow — no compatible GCP family in catalog for this profile.",
            "candidates": [],
        }

    scored = apply_hard_constraints(profile, pool)
    scored = score_candidates(profile, scored)

    accepted = sorted([s for s in scored if s.accepted], key=lambda s: s.score, reverse=True)
    rejected = [s for s in scored if not s.accepted]

    if not accepted:
        return {
            "instance_type": profile["instance_type"],
            "recommendation": None,
            "mapping_confidence": 0.0,
            "classification_confidence": profile.get("classification_confidence"),
            "status": "all_candidates_rejected",
            "note": "Route to outlier flow — every candidate failed a hard constraint.",
            "candidates": [
                {"family": s.candidate.family, "accepted": False,
                 "checks": s.checks, "reason": s.rejection_reason}
                for s in rejected
            ],
        }

    winner = accepted[0]
    alternates = accepted[1:]

    return {
        "instance_type": profile["instance_type"],
        "recommendation": winner.candidate.family,
        "recommended_size": {"vcpu": winner.sized_vcpu, "memory_gb": winner.sized_memory_gb},
        "mapping_confidence": winner.score,
        "classification_confidence": profile.get("classification_confidence"),
        "status": "mapped",
        "candidates": (
            [{"family": winner.candidate.family, "accepted": True, "selected": True,
              "score": winner.score, "checks": winner.checks, "reasons": winner.reasons}]
            + [{"family": a.candidate.family, "accepted": True, "selected": False,
                "score": a.score, "checks": a.checks, "reasons": a.reasons} for a in alternates]
            + [{"family": r.candidate.family, "accepted": False,
                "checks": r.checks, "reason": r.rejection_reason} for r in rejected]
        ),
    }


# ---------------------------------------------------------------------------
# resolve_compute_family — standalone helper for single EC2 instance lookups
# ---------------------------------------------------------------------------

def resolve_compute_family(
    instance_type: str,
    arch: str,
    vcpu: int,
    ram_gb: int,
    local_nvme_gb: int = 0,
    efa: bool = False,
) -> dict:
    """
    Wraps the full candidate pipeline for one EC2 instance type.

    Loads instance-family-map.json to get the base family, then scores
    candidates via get_candidate_pool / apply_hard_constraints / score_candidates.

    Returns a dict with keys:
      family, sized_vcpu, sized_mem_gb, score, tier, rejected_candidates

    Falls back to the instance-family-map result (with score=0 and no sizing)
    when no candidates survive hard-constraint filtering.
    """
    # ── Load instance-family-map ──────────────────────────────────────────
    family_map_path = os.path.join(_DATA_DIR, "instance-family-map.json")
    try:
        with open(family_map_path) as f:
            family_map = json.load(f)
    except (FileNotFoundError, OSError):
        family_map = {}

    # Determine base GCP family from family-map, falling back to ratio rules.
    # instance-family-map.json is the authoritative source; the fallback here
    # mirrors its logic but also uses the actual RAM/vCPU ratio so memory-
    # optimized workloads (r-family, 8+ GB/vCPU) land on N2/C4A-highmem rather
    # than N2D (4 GB/vCPU), which would double vCPUs and inflate cost.
    base_family = family_map.get("examples", {}).get(instance_type) or family_map.get(instance_type)
    if not base_family:
        prefix = instance_type.split(".")[0]
        # arm64 rules from instance-family-map.json
        arm64_preferred = family_map.get("rules", {}).get("arm64_preferred", "C4A")
        if arch == "arm64":
            ram_per_vcpu = ram_gb / max(vcpu, 1)
            # r6g/r7g Graviton: 8 GB/vCPU → C4A highmem
            if ram_per_vcpu >= 7:
                base_family = "C4A_HIGHMEM"
            else:
                base_family = arm64_preferred  # C4A standard (4 GB/vCPU)
        elif any(prefix.startswith(b) for b in ("t2", "t3", "t3a")):
            base_family = "E2"
        else:
            # Choose x86 family by RAM/vCPU ratio to avoid over-provisioning
            ram_per_vcpu = ram_gb / max(vcpu, 1)
            if ram_per_vcpu >= 12:
                base_family = "M3"    # x1e/x2i ≥16 GB/vCPU → M3 ultramem
            elif ram_per_vcpu >= 6:
                base_family = "N2"    # r5/r6i/r7i 8 GB/vCPU → N2 standard
            else:
                base_family = "N2D"   # c/m family ≤4 GB/vCPU → N2D (best price)

    # ── Build a minimal profile dict ─────────────────────────────────────
    profile = {
        "service": "EC2",
        "instance_type": instance_type,
        "architecture": arch,
        "vcpu": vcpu,
        "memory_gb": ram_gb,
        "ram_gb": ram_gb,
        "gpu": False,
        "local_nvme_gb": local_nvme_gb,
        "efa_supported": efa,
        "primary_strategy": (
            "general_purpose_arm" if arch == "arm64" else
            "burstable" if any(instance_type.startswith(p) for p in ("t2.", "t3.", "t4g.")) else
            "general_purpose"
        ),
        "secondary_strategies": [],
        "network_tier": "efa" if efa else "standard",
        "required_validations": (
            (["local_nvme"] if local_nvme_gb > 0 else []) +
            (["network"] if efa else [])
        ),
    }

    # ── Run the candidate pipeline ────────────────────────────────────────
    families_catalog = load_candidate_families()

    # Build a minimal catalog entry from the built-in family definitions so the
    # pipeline has something to score even without a full YAML catalog.
    builtin_families = _BUILTIN_CANDIDATE_FAMILIES
    family_key = base_family.upper() if base_family and base_family.upper() in builtin_families else (
        "C4A" if arch == "arm64" else "N2D"
    )
    fdef = builtin_families[family_key]

    synthetic_candidate = Candidate(
        family=fdef["family"],
        architecture=fdef["architecture"],
        generation=2,
        local_nvme=fdef.get("local_nvme", False),
        network_tier=fdef.get("network_tier", "standard"),
        gb_per_vcpu=float(fdef.get("ram_per_vcpu", 4)),
        vcpu_min=fdef.get("min_vcpu", 2),
        vcpu_max=fdef.get("max_vcpu", 224),
    )

    # Try the full catalog first; fall back to the synthetic single-candidate pool.
    try:
        pool = get_candidate_pool(profile, families_catalog, {})
    except Exception:
        pool = []

    if not pool:
        pool = [synthetic_candidate]

    scored = apply_hard_constraints(profile, pool)
    scored = score_candidates(profile, scored)

    accepted = sorted([s for s in scored if s.accepted], key=lambda s: s.score, reverse=True)
    rejected = [s for s in scored if not s.accepted]

    if accepted:
        winner = accepted[0]
        return {
            "family": winner.candidate.family,
            "sized_vcpu": winner.sized_vcpu,
            "sized_mem_gb": winner.sized_memory_gb,
            "score": winner.score,
            "tier": 1 if winner.score >= 0.7 else 2 if winner.score >= 0.4 else 3,
            "rejected_candidates": [
                {"family": r.candidate.family, "checks": r.checks}
                for r in rejected
            ],
        }

    # All candidates rejected — fall back to family-map result with no sizing
    return {
        "family": fdef["family"],
        "sized_vcpu": None,
        "sized_mem_gb": None,
        "score": 0.0,
        "tier": 3,
        "rejected_candidates": [
            {"family": r.candidate.family, "checks": r.checks}
            for r in rejected
        ],
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    families_catalog = load_candidate_families("candidate_families.yaml")
    gpu_catalog = load_gpu_candidates("gpu_candidates.yaml")

    demo_profiles = [
        {
            "service": "EC2", "instance_type": "r5dn.4xlarge",
            "architecture": "x86_64", "vcpu": 16, "memory_gb": 128,
            "gpu": False,
            "primary_strategy": "memory_optimized",
            "secondary_strategies": ["storage_optimized"],
            "network_tier": "high",
            "required_validations": ["architecture", "local_nvme", "network"],
            "classification_confidence": 1.0,
        },
        {
            # p4d.24xlarge: 8x A100 40GB
            "service": "EC2", "instance_type": "p4d.24xlarge",
            "architecture": "x86_64", "vcpu": 96, "memory_gb": 1152,
            "gpu": True, "gpu_model": "A100", "gpu_vram_gb": 40, "gpu_count": 8,
            "primary_strategy": "gpu", "secondary_strategies": [],
            "network_tier": "efa",
            "required_validations": ["architecture"],
            "classification_confidence": 1.0,
        },
        {
            # p4de.24xlarge: same node, 80GB A100s — should NOT collapse to the same
            # candidate as p4d above, this is exactly the VRAM-disambiguation fix.
            "service": "EC2", "instance_type": "p4de.24xlarge",
            "architecture": "x86_64", "vcpu": 96, "memory_gb": 1152,
            "gpu": True, "gpu_model": "A100", "gpu_vram_gb": 80, "gpu_count": 8,
            "primary_strategy": "gpu", "secondary_strategies": [],
            "network_tier": "efa",
            "required_validations": ["architecture"],
            "classification_confidence": 1.0,
        },
        {
            "service": "EC2", "instance_type": "c6g.8xlarge",
            "architecture": "arm64", "vcpu": 32, "memory_gb": 64,
            "gpu": False,
            "primary_strategy": "compute_optimized_arm", "secondary_strategies": [],
            "network_tier": "standard",
            "required_validations": ["architecture"],
            "classification_confidence": 1.0,
        },
        {
            # g4dn uses T4 — no exact GCP equivalent, should land on g2-standard
            # with exact_match=false and a visibly lower mapping_confidence.
            "service": "EC2", "instance_type": "g4dn.2xlarge",
            "architecture": "x86_64", "vcpu": 8, "memory_gb": 32,
            "gpu": True, "gpu_model": "T4", "gpu_vram_gb": 16, "gpu_count": 1,
            "primary_strategy": "gpu", "secondary_strategies": [],
            "network_tier": "standard",
            "required_validations": ["architecture"],
            "classification_confidence": 1.0,
        },
        {
            # p3.2xlarge uses V100 — no GCP equivalent at all, should force outlier flow.
            "service": "EC2", "instance_type": "p3.2xlarge",
            "architecture": "x86_64", "vcpu": 8, "memory_gb": 61,
            "gpu": True, "gpu_model": "V100", "gpu_vram_gb": 16, "gpu_count": 1,
            "primary_strategy": "gpu", "secondary_strategies": [],
            "network_tier": "standard",
            "required_validations": ["architecture"],
            "classification_confidence": 1.0,
        },
    ]

    for profile in demo_profiles:
        result = recommend(profile, families_catalog, gpu_catalog)
        print(f"\n=== {profile['instance_type']} ===")
        print(json.dumps(result, indent=2))
