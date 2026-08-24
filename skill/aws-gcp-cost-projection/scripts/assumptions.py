"""
assumptions.py  —  Phase Q + Phase X: Migration Assumptions & Unsupported Features.

CUR is a billing dataset. It tells you what AWS charged for — it doesn't tell
you whether the workload uses AVX-512, whether binaries are architecture-specific,
whether the app depends on Nitro Enclaves, or whether the Docker images are
multi-arch. Rather than silently treating those as non-issues, the engine emits
explicit assumptions for every one of them.

The customer sees: "We assumed X because CUR cannot verify Y."
That's far safer than: [nothing mentioned, customer assumes we checked].

Two registries:

  STRATEGY_ASSUMPTIONS  — per workload strategy, what we're assuming about
                          application behavior that CUR cannot confirm

  UNSUPPORTED_FEATURES  — AWS features that have no direct GCP equivalent or
                          can't be automatically mapped; always flag + penalize
                          confidence, never silently ignore
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Assumption:
    text: str
    confidence_impact: float = 0.0  # reduction in overall confidence if this assumption is wrong
    requires_manual_review: bool = False


@dataclass
class UnsupportedFeature:
    feature: str
    impact: str
    confidence_penalty: float
    action: str


# ---------------------------------------------------------------------------
# Per-strategy assumptions
# ---------------------------------------------------------------------------

STRATEGY_ASSUMPTIONS: dict[str, list[Assumption]] = {

    "general_purpose": [
        Assumption("No architecture-specific compiled binaries (workload is portable across CPU generations)."),
        Assumption("Application does not depend on Intel-specific instruction sets (AVX-512, AMX) "
                   "beyond what GCP's comparable generation provides."),
        Assumption("No dependency on AWS Nitro System features (e.g., Nitro Enclaves, "
                   "Nitro Security Chip) that have no GCP equivalent."),
    ],

    "general_purpose_arm": [
        Assumption("Application and all dependencies are compiled for or compatible with ARM64 "
                   "(aarch64). Container images are multi-arch or ARM64-native.",
                   confidence_impact=0.05, requires_manual_review=True),
        Assumption("No third-party libraries or agents that are x86-only are in use.",
                   confidence_impact=0.03, requires_manual_review=True),
        Assumption("No AWS Graviton-specific optimizations (e.g., Graviton-tuned kernel flags) "
                   "are required on GCP Axion — performance characteristics may differ."),
    ],

    "compute_optimized": [
        Assumption("Workload is stateless or application-level statefulness is handled externally "
                   "(object storage, database). Local disk is ephemeral."),
        Assumption("No dependency on sustained turbo-boost performance above the GCP candidate's "
                   "baseline clock (CUR does not report average CPU frequency)."),
    ],

    "compute_optimized_arm": [
        Assumption("Application and all dependencies are compiled for ARM64. See general_purpose_arm "
                   "assumptions above.",
                   confidence_impact=0.05, requires_manual_review=True),
    ],

    "memory_optimized": [
        Assumption("In-memory dataset size fits within the recommended GCP shape's RAM. "
                   "CUR does not report actual memory utilization — this is assumed from instance "
                   "class selection, not measured data.",
                   confidence_impact=0.03),
        Assumption("NUMA topology sensitivity: if the workload is NUMA-aware and pinned to specific "
                   "NUMA nodes, GCP NUMA configuration should be verified. CUR cannot report this.",
                   confidence_impact=0.02, requires_manual_review=False),
        Assumption("Memory bandwidth requirements are met by the GCP candidate's DDR generation. "
                   "Generation is inferred from instance family; exact bandwidth is not in CUR."),
    ],

    "storage_optimized": [
        Assumption("Local NVMe on AWS instance is used for ephemeral/scratch data, not persistent "
                   "storage. If used for persistent data, a storage migration plan is required "
                   "before GCP Local SSD can replace it.",
                   confidence_impact=0.05, requires_manual_review=True),
        Assumption("IOPS and throughput requirements are met by the GCP candidate's Local SSD "
                   "performance class. CUR provisioned IOPS (if present) used for sizing; "
                   "actual utilization unknown.",
                   confidence_impact=0.02),
    ],

    "burstable": [
        Assumption("Workload sustains CPU usage within the T-series baseline most of the time. "
                   "CUR does not report actual CPU utilization; credit consumption line items "
                   "used as a proxy.",
                   confidence_impact=0.05),
        Assumption("T-series unlimited mode status unknown (requires EC2 DescribeInstances API). "
                   "If unlimited mode is enabled, the customer may be accepting sustained CPU "
                   "charges beyond credits — GCP shared-core shapes do not have this mechanism.",
                   confidence_impact=0.03, requires_manual_review=True),
        Assumption("GCP shared-core shapes (e2-micro/small/medium) have a different burst model "
                   "than AWS T-series CPU credits. Sustained-load performance may differ."),
    ],

    "gpu": [
        Assumption("CUDA workloads target NVIDIA hardware specifically. Axion (ARM) is excluded. "
                   "Architecture mismatch check enforces this.",
                   confidence_impact=0.0),
        Assumption("CUDA version compatibility: CUR does not report the CUDA driver or toolkit "
                   "version installed. Assumed compatible with GCP's target GPU generation.",
                   confidence_impact=0.05, requires_manual_review=True),
        Assumption("MIG (Multi-Instance GPU) configuration unknown if used on AWS. "
                   "GCP A3/A2 support MIG but configuration must be validated.",
                   confidence_impact=0.03, requires_manual_review=True),
        Assumption("NVLink topology: p5/p4d AWS instances use NVLink for GPU-to-GPU communication. "
                   "GCP A3 also uses NVLink (H100). Topology validated by GPU model match; "
                   "bandwidth specifications should be confirmed independently."),
        Assumption("GPU driver version: CUR does not report driver version. "
                   "Assumed compatible with GCP's Compute Engine GPU driver image for target model.",
                   confidence_impact=0.02),
    ],

    "database": [
        Assumption("Database engine version supported by Cloud SQL / AlloyDB in the target region. "
                   "Major version match confirmed from CUR; minor version compatibility assumed.",
                   confidence_impact=0.03),
        Assumption("Parameter group settings, custom extensions, and stored procedures have not "
                   "been audited. CUR cannot report these. Functional parity must be validated "
                   "separately.",
                   confidence_impact=0.05, requires_manual_review=True),
        Assumption("Backup and replication topology assumed from CUR Multi-AZ flag only. "
                   "Read replica count and backup retention unknown (require RDS Describe* API).",
                   confidence_impact=0.02),
    ],
}


# ---------------------------------------------------------------------------
# Unsupported features registry  (Phase X)
# ---------------------------------------------------------------------------

UNSUPPORTED_FEATURES: dict[str, UnsupportedFeature] = {
    "nitro_enclaves": UnsupportedFeature(
        feature="AWS Nitro Enclaves",
        impact="No equivalent on GCP. Workloads requiring hardware-isolated enclaves for "
               "sensitive data processing must be rearchitected (e.g., GCP Confidential VMs "
               "provide a different trust model but are not a direct replacement).",
        confidence_penalty=0.15,
        action="Manual assessment required — flag for security architecture review.",
    ),
    "efa": UnsupportedFeature(
        feature="Elastic Fabric Adapter (EFA)",
        impact="GCP equivalent is high-bandwidth networking on A3/HPC shapes. "
               "EFA-dependent MPI workloads may need validation on GCP's networking fabric.",
        confidence_penalty=0.05,
        action="Verify MPI/NCCL performance on GCP equivalent networking before recommending.",
    ),
    "dedicated_hosts": UnsupportedFeature(
        feature="AWS Dedicated Hosts",
        impact="GCP Sole-Tenant Nodes are the equivalent. Licensing and tenancy isolation "
               "must be reconfigured. CUR purchase model field used to detect this.",
        confidence_penalty=0.10,
        action="Route to outlier flow — Sole-Tenant Node sizing requires manual configuration.",
    ),
    "outposts": UnsupportedFeature(
        feature="AWS Outposts",
        impact="On-premises AWS infrastructure. No GCP on-premises equivalent in CUR scope. "
               "Google Distributed Cloud is the functional analog but requires separate scoping.",
        confidence_penalty=0.30,
        action="Out of scope for automated CUR-based mapping. Flag for separate assessment.",
    ),
    "local_zones": UnsupportedFeature(
        feature="AWS Local Zones",
        impact="Latency-sensitive edge workloads. GCP equivalent is Edge Zones (limited "
               "availability). Region mapping may not be 1:1.",
        confidence_penalty=0.10,
        action="Manual region mapping required — check GCP Edge Zone availability.",
    ),
    "fpga": UnsupportedFeature(
        feature="FPGA (F-family instances)",
        impact="GCP has no general-purpose FPGA compute offering. Workloads using FPGAs "
               "for acceleration must be rearchitected or moved to a different provider.",
        confidence_penalty=0.40,
        action="Route to outlier flow — no automatic GCP mapping exists for FPGA workloads.",
    ),
    "mac_instances": UnsupportedFeature(
        feature="Mac instances (mac1/mac2)",
        impact="GCP has no macOS compute offering. Xcode/iOS build pipelines running on "
               "AWS Mac instances cannot be directly migrated to GCP.",
        confidence_penalty=0.50,
        action="Route to outlier flow — no GCP equivalent. Consider Orka or other Mac CI providers.",
    ),
    "inferentia": UnsupportedFeature(
        feature="AWS Inferentia (inf1/inf2)",
        impact="GCP equivalent is Cloud TPU for inference, or A2/G2 GPU shapes with TensorRT. "
               "Neuron SDK code must be rewritten for TensorFlow/PyTorch on GCP accelerators.",
        confidence_penalty=0.20,
        action="Route to outlier flow — Neuron SDK not compatible with GCP. ML framework migration required.",
    ),
    "trainium": UnsupportedFeature(
        feature="AWS Trainium (trn1/trn2)",
        impact="GCP equivalent is Cloud TPU v4/v5 for training workloads. Neuron SDK "
               "training code must be migrated to JAX/TensorFlow/PyTorch on TPU.",
        confidence_penalty=0.20,
        action="Route to outlier flow — TPU migration requires application-level changes.",
    ),
    "wavelength": UnsupportedFeature(
        feature="AWS Wavelength Zones",
        impact="Ultra-low latency 5G edge compute. GCP has no equivalent offering in current scope.",
        confidence_penalty=0.35,
        action="Out of scope for automated mapping. Flag for separate assessment.",
    ),
}


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def emit_assumptions(
    profile: dict,
    contract_unavailable_fields: list[str] | None = None,
) -> dict:
    """
    Returns the full assumptions block for a profile.
    Includes strategy-based assumptions + any unsupported features detected.
    """
    strategy = profile.get("primary_strategy", "general_purpose")
    assumptions = list(STRATEGY_ASSUMPTIONS.get(strategy, []))

    # Detect unsupported features from profile flags
    triggered_features: list[UnsupportedFeature] = []
    total_confidence_penalty = 0.0

    if profile.get("bare_metal"):
        triggered_features.append(UNSUPPORTED_FEATURES.get("dedicated_hosts",
            UnsupportedFeature("Bare Metal", "Requires sole-tenant or bare-metal GCP node.",
                               0.10, "Manual sizing required.")))

    if profile.get("efa_supported") and strategy == "gpu":
        uf = UNSUPPORTED_FEATURES["efa"]
        triggered_features.append(uf)
        total_confidence_penalty += uf.confidence_penalty

    # Derive from instance_type prefix for families not caught by catalog classification
    instance_type = profile.get("instance_type", "")
    if instance_type.startswith(("inf1", "inf2")):
        uf = UNSUPPORTED_FEATURES["inferentia"]
        triggered_features.append(uf)
        total_confidence_penalty += uf.confidence_penalty
    elif instance_type.startswith(("trn1", "trn2")):
        uf = UNSUPPORTED_FEATURES["trainium"]
        triggered_features.append(uf)
        total_confidence_penalty += uf.confidence_penalty
    elif instance_type.startswith("mac"):
        uf = UNSUPPORTED_FEATURES["mac_instances"]
        triggered_features.append(uf)
        total_confidence_penalty += uf.confidence_penalty
    elif instance_type.startswith("f1"):
        uf = UNSUPPORTED_FEATURES["fpga"]
        triggered_features.append(uf)
        total_confidence_penalty += uf.confidence_penalty

    # Fields that were UNAVAILABLE in the contract propagate as explicit assumptions
    data_assumptions = []
    for fname in (contract_unavailable_fields or []):
        data_assumptions.append(
            f"{fname}: not available from CUR — value assumed/unknown, "
            f"verify independently if this field is critical for the workload."
        )

    manual_review_required = (
        any(a.requires_manual_review for a in assumptions)
        or bool(triggered_features)
    )

    return {
        "strategy_assumptions": [a.text for a in assumptions],
        "data_assumptions": data_assumptions,
        "unsupported_features": [
            {
                "feature":             uf.feature,
                "impact":              uf.impact,
                "action":              uf.action,
                "confidence_penalty":  uf.confidence_penalty,
            }
            for uf in triggered_features
        ],
        "total_confidence_penalty_from_features": round(total_confidence_penalty, 3),
        "manual_review_required": manual_review_required,
    }
