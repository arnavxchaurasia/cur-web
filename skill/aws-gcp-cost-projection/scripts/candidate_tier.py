"""
candidate_tier.py  —  Phase L: Candidate Tiering.

Inserts a tiering step between hard-constraint filtering and scoring.
Candidates are sorted into four tiers; scoring (price, generation, capacity fit)
only compares candidates within the same tier. The engine always prefers the
best Tier 1 over any Tier 2, even if a Tier 2 candidate is cheaper.

This fixes the failure mode where a Compromise candidate beats a Perfect one
purely because GCP priced it lower — which gives the customer a worse
technical recommendation disguised as "optimized cost."

Tier definitions:

  PERFECT      All critical dimensions match exactly: architecture, workload
               family alignment, storage/network requirements met, GPU model
               and VRAM exact (if applicable). No compromise on any axis.

  EQUIVALENT   Architecture preserved and no capacity shortfall, but minor
               compromises: family is adjacent (e.g., general-purpose instead
               of compute-optimized for a workload where the delta is small),
               OR nearest-fit GPU (same generation, different model), OR minor
               over-provisioning (up to 2× on one dimension).

  COMPROMISE   Architecture preserved but meaningful compromises elsewhere:
               storage or network requirements relaxed, significant family
               mismatch, or GPU substitute is a different generation entirely.

  UNSUPPORTED  No valid automatic mapping exists. Forces the outlier flow.
               (In practice, hard-constraint filtering already catches most of
               these — this tier exists for candidates that passed constraints
               but have exact_gpu_match=False with a very different VRAM class,
               or other cases where the match is technically valid but not
               production-safe without manual review.)
"""

from __future__ import annotations
from enum import IntEnum

NETWORK_RANK = {"standard": 0, "high": 1, "efa": 2}


class Tier(IntEnum):
    PERFECT      = 1
    EQUIVALENT   = 2
    COMPROMISE   = 3
    UNSUPPORTED  = 4

    def label(self) -> str:
        return {1: "Perfect", 2: "Equivalent", 3: "Compromise", 4: "Unsupported"}[self.value]


def assign_tier(
    profile: dict,
    family: str,
    architecture: str,
    local_nvme: bool,
    network_tier: str,
    exact_gpu_match: bool,
    gpu_vram_gb: float | None,
    sized_vcpu: int | None,
    sized_memory_gb: float | None,
    workload_family_class: str | None = None,  # the GCP family's workload class, if known
) -> Tier:
    """
    Assigns a tier to a single candidate that has already passed hard constraints.

    Inputs come from both the profile (what the customer needs) and the candidate
    (what GCP offers). The workload_family_class parameter is optional — if the
    catalog provides it, it enables Tier 1 family-alignment checks.
    """
    req_arch    = profile.get("architecture", "x86_64")
    req_nvme    = "local_nvme" in profile.get("required_validations", [])
    req_network = profile.get("network_tier", "standard")
    req_vcpu    = profile.get("vcpu", 0)
    req_mem     = profile.get("memory_gb", 0)
    is_gpu      = profile.get("gpu", False)

    # Architecture mismatch after hard constraints shouldn't happen, but
    # if it does, force Unsupported rather than silently slipping through.
    if architecture != req_arch:
        return Tier.UNSUPPORTED

    # GPU exact-match check
    gpu_ok = True
    gpu_vram_ok = True
    if is_gpu:
        if not exact_gpu_match:
            gpu_ok = False
        if gpu_vram_gb is not None and profile.get("gpu_vram_gb") is not None:
            # Allow ±5GB tolerance (accounts for reported vs. usable VRAM)
            if abs(gpu_vram_gb - profile["gpu_vram_gb"]) > 5:
                gpu_vram_ok = False

    # Storage and network
    nvme_ok    = (not req_nvme) or local_nvme
    network_ok = NETWORK_RANK.get(network_tier, 0) >= NETWORK_RANK.get(req_network, 0)

    # Over-provisioning (candidate sized significantly above requirement)
    over_provisioned = False
    if sized_vcpu and req_vcpu:
        over_provisioned = over_provisioned or (sized_vcpu > req_vcpu * 2.5)
    if sized_memory_gb and req_mem:
        over_provisioned = over_provisioned or (sized_memory_gb > req_mem * 2.5)

    # Family alignment — if the GCP catalog provides a workload class tag,
    # check whether it matches the profile's primary strategy.
    family_aligned = True
    if workload_family_class and profile.get("primary_strategy"):
        strategy = profile["primary_strategy"]
        # Loose alignment: memory strategies should map to highmem families, etc.
        STRATEGY_FAMILY_MAP = {
            "memory_optimized":    {"highmem", "ultramem", "memory"},
            "compute_optimized":   {"standard", "highcpu", "compute"},
            "compute_optimized_arm": {"standard", "highcpu"},
            "storage_optimized":   {"highmem", "standard"},
            "general_purpose":     {"standard"},
            "general_purpose_arm": {"standard"},
            "burstable":           {"micro", "small", "medium"},
            "gpu":                 {"gpu", "a2", "a3", "g2"},
        }
        allowed = STRATEGY_FAMILY_MAP.get(strategy, set())
        if allowed:
            # Check if any allowed keyword is a substring of the GCP family name
            family_aligned = any(kw in family.lower() for kw in allowed)

    # ── Tier assignment ────────────────────────────────────────────────────

    # Unsupported: GPU with very different VRAM class
    if is_gpu and not gpu_vram_ok:
        return Tier.UNSUPPORTED

    # Perfect: everything lines up
    if (gpu_ok and nvme_ok and network_ok and family_aligned and not over_provisioned):
        return Tier.PERFECT

    # Equivalent: architecture preserved, GPU nearest-fit or minor family mismatch
    if (nvme_ok and network_ok and not over_provisioned):
        return Tier.EQUIVALENT

    # Compromise: something meaningful relaxed
    if architecture == req_arch:
        return Tier.COMPROMISE

    return Tier.UNSUPPORTED


def pick_best(tiered_candidates: list[dict]) -> dict | None:
    """
    Given a list of dicts (each with 'tier' and 'score'), returns the best
    candidate: lowest tier value first, then highest score within that tier.
    Never promotes a Tier 3 candidate above a Tier 1 one regardless of score.
    """
    if not tiered_candidates:
        return None
    return min(
        tiered_candidates,
        key=lambda c: (c["tier"], -c["score"])
    )
