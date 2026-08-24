Here's the audit result — 7 logic inconsistencies found in the Layer 1–4 code, ordered by severity, with the exact fix for each. Hand this to the implementation model as-is.

Plan: Fix logic inconsistencies in Layer 1–4 global logic
1. validate_fix.py — Layer 4 autofixes run AFTER the gates that check the same condition (CRITICAL)
Problem: The existing unreachable_rate gate (~line 831) captures violations, then my new map-with-no-rate autofix (line ~1038) downgrades those same rows to passthrough. The violation list was already recorded, so the run fails on violations that were just fixed. Same bug with the echo gate: it records gcp_service_echo violations into the report, then the autofix clears them — but n_hard still counts them → exit 1 every time an echo exists, making the autofix pointless.

Fix: Move both new autofixes (map-no-rate downgrade, echo-clear) up into the AUTOFIX section (before # ---------- GATES ----------, ~line 788). The gates then run on post-fix state and act as pure checks: if the echo/no-rate gates still find rows after autofix, that's a legitimate failure. In --check-only mode autofixes are skipped, so gates still catch problems in the watcher backstop — that behavior stays correct.

2. aws_normalizer.py — substring fallback loop matches in dict order, causing wrong canonical keys (HIGH)
Problem: canonical_service() falls back to for alias, key in _ALIASES.items(): if alias in full_lower. Dict order = insertion order, so short aliases win over longer ones:

"ec2" matches before "ec2 container registry" → ECR is normalized to ec2
"s3" could match inside unrelated strings; "waf" before "wafv2" (harmless but same class)
"config" matches "AWS Config" but also any product containing "config"
Fix: In the fallback loop, sort aliases by length descending (sorted(_ALIASES.items(), key=lambda kv: -len(kv[0]))) so the most specific alias wins. Additionally, require short aliases (≤4 chars: ec2, s3, ebs, kms, waf, ses, sns, sqs, dax, acm, sso, emr, msk, eks, ecs, ecr, elb, vpc, vpn, tax) to match on word boundaries (re.search(r'\b'+re.escape(alias)+r'\b', full_lower)) instead of raw substring.

3. aws_normalizer.py — dead dict key typo (LOW but trivial)
Problem: "amazongUardduty": "guardduty" — the key contains an uppercase U but lookups compare against lowercased text, so this key can never match.

Fix: Change to "amazonguardduty".

4. apply_static_mappings.py — dashboard detection only checks usage_type; PDF bills have empty usage_type (HIGH)
Problem: is_dashboard = "dashboardhour" in ut or "dashboard" in ut. The 3.3x DashboardHour over-projection in job f42d201b was a PDF bill where usage_type is empty and the charge info lives in the product/operation fields. The fix I made would not fire on the exact bill that exposed the bug.

Fix: Build a blob like the Fargate fix in the same file: blob = product + " " + ut + " " + op (all lowercased) and check "dashboard" in blob.

5. orchestrate.go — verify Phase 1 actually executes PostLLMScripts (VERIFY, possibly HIGH)
Problem: I added aws_normalizer.py as a PostLLMScripts entry in Phase 1, but Phase 1 is a plain-script phase (Script: "scripts/ingest.py", no LLM). It is unverified whether the orchestrator's run loop executes PostLLMScripts for script-only phases — if the loop only runs them around the agy/LLM invocation, the normalizer never runs.

Fix: Read the phase-execution function in orchestrate.go (and any related runner in internal/jobs/). If PostLLMScripts don't run for script phases, move the normalizer call to Phase 2's PreLLMScripts, as the first entry before classify_mechanics.py (it must precede classification so classification can eventually key on canonical_service). Rebuild/restart the Go server after (go run ./cmd/server per Makefile).

6. validate_fix.py — null_gcp_service gate ordering vs echo autofix (MEDIUM)
Problem: The echo autofix sets gcp_service='Manual Review' — fine. But if the autofixes are moved before the gates (item 1), confirm the null_gcp_service gate runs after echo-clear so the reset rows (which now have 'Manual Review', not NULL) don't trip it. Also passthrough_budget runs on a snapshot taken before the new downgrades add passthrough spend — after reordering (item 1), the budget gate will correctly see the downgraded rows; be aware this can newly fail bills with big no-rate spend. That's intended (loud, not silent), but the failure message should point at the [validator: map-with-no-rate downgraded] note so the operator knows why.

Fix: Ordering falls out of item 1 automatically; add one sentence to the passthrough_budget violation message: include a count of rows carrying the map-with-no-rate note if > 0.

7. service_map.json — "certificate manager" rule mode/reason mismatch (LOW)
Problem: New rule has "mode": "ignore" (forces $0) but the reason text says "Passthrough or ignore depending on ACM Private CA vs public cert." ACM Private CA is $400/mo real spend — forcing $0 hides it.

Fix: Change mode to "review" (passthrough, carries AWS cost) and reword the reason: "ACM public certs free on both clouds (usually $0 rows anyway); Private CA $400/mo has GCP CAS equivalent (~$20–200/mo tier) — passthrough until CAS pricer exists." Passthrough of a $0 row is still $0, so nothing is lost for the common case.

Verification steps (after all fixes)
python3 -m pytest tests/ -v from skill/aws-gcp-cost-projection/ — all existing tests must pass.
python3 -c "from scripts.aws_normalizer import canonical_service; assert canonical_service('Amazon EC2 Container Registry') == 'ecr'; assert canonical_service('Amazon Elastic Container Service APS3-Fargate-GB-Hours') == 'ecs'" (note: Fargate-in-suffix normalizes to ecs because the suffix is stripped before matching — decide whether Fargate charge-hint extraction should override this; if yes, check for fargate in the raw product string before suffix-stripping and return fargate).
Syntax-compile all touched Python files.
Re-run the f42d201b bill through the pipeline and confirm: DashboardHour rows show $0, no unreachable_rate failures for rows that got downgraded, normalizer prints its unrecognized-products table in the job log.
Item 2's word-boundary list and item 5's verification are the two spots where the implementation model should be most careful — everything else is mechanical.

Please do it per SKU also
Here's the per-SKU (service × charge-type) pricing matrix — the Layer 3 deliverable. Each row is a declared, auditable pricing decision. Status says whether the current code already does this, does it wrong, or doesn't do it at all — the implementation model works through every row that isn't ✅.

Layer 3 — Per-SKU Pricing Matrix
Decision types: SKU = deterministic GCP SKU + multiplier · FORMULA = derived conversion · PASS = passthrough at AWS cost with GCP label · IGNORE = $0 with reason.

Compute
Charge type	AWS unit	Decision	GCP target / formula	Status
EC2 instance-hours (x86)	Hrs	SKU	family_mapper → N2/N2D/C2/C3/E2 core+ram breakdown	✅ works
EC2 instance-hours (Graviton)	Hrs	SKU	C4A/T2A with "Arm" infix in SKU desc	✅ fixed (ARM infix bug)
EC2 GPU (g4/g5/g6, p3-p5)	Hrs	SKU	_gpu_profile() → T4/L4/A100/H100 attached GPUs	✅ works
EC2 Inferentia/Trainium	Hrs	PASS	"No GCP equivalent — TPU is analogue, needs porting"	✅ works
EC2 Spot	Hrs	SKU	Same family, Spot/Preemptible rate — verify not matching Preemptible SKU for OD rows (past bug)	✅ guarded
CPU credits (t-family)	vCPU-mo	IGNORE	E2 has no burst-credit charge	✅ service_map
Public IPv4	Hrs	SKU	External IP charge per IP-hour — currently mode=review (PASS)	⚠️ upgrade PASS→SKU: "Static Ip Charge" SKU exists in catalog
EBS gp2/gp3 capacity	GB-Mo	SKU	Hyperdisk Balanced / pd-balanced	✅ works (0.91x verified)
EBS gp3 provisioned IOPS	IOPS-Mo	IGNORE	Included in pd-balanced capacity price	✅ works
EBS io1/io2 + IOPS	GB-Mo + IOPS-Mo	SKU	pd-extreme capacity + provisioned-IOPS SKU (GCP does bill IOPS on extreme)	⚠️ verify io2 IOPS rows aren't ignored like gp3 — different rule
EBS snapshots	GB-Mo	SKU	PD Snapshot storage	✅ works
Containers & Serverless
Charge type	AWS unit	Decision	GCP target / formula	Status
Fargate vCPU-Hours	Hrs	SKU	Autopilot mCPU × 1000, 4A90-D02B-3BAC-class SKUs	⚠️ mapper fixed; rates must load — add Autopilot SKUs to prefetch or verify incremental_rerate picks them up next run
Fargate GB-Hours	Hrs	SKU	Autopilot Pod Memory Requests × 1.0	⚠️ same
Fargate ARM (vCPU/GB)	Hrs	SKU	Autopilot Arm Pod CPU/Memory	⚠️ same
ECS management fee	—	IGNORE	ECS control plane is free; GKE Standard fee captured under EKS rows	❌ missing — currently PASS via service_map
EKS cluster-hours	Hrs	SKU	GKE cluster management fee $0.10/hr, 1:1	⚠️ currently mode=review PASS at $143 — parity is exact, upgrade to SKU
Lambda GB-seconds	Lambda-GB-Second	FORMULA	Cloud Run: memory GiB-sec ×1.0 + vCPU-sec = GB-sec × (cpu/mem ratio 1vCPU:2GiB → ×0.5)	⚠️ LLM does this today, ~0.5x ratio observed; make static (roadmap T8/P3-A)
Lambda requests	Requests	SKU	Cloud Run Requests $0.40/M vs AWS $0.20/M	❌ missing — goes to LLM
Lambda provisioned concurrency	Hrs	PASS	No clean Cloud Run min-instances price mapping without instance size	❌ missing
Storage
Charge type	AWS unit	Decision	GCP target / formula	Status
S3 Standard storage	GB-Mo	SKU	GCS Standard Storage	✅ works
S3 Standard-IA / One Zone-IA	GB-Mo	SKU	GCS Nearline	✅ works
S3 Glacier Instant/Flexible	GB-Mo	SKU	GCS Coldline (pinned SKU — no word-overlap)	✅ fixed (120x bug)
S3 Glacier Deep Archive	GB-Mo	SKU	GCS Archive (compact-key fix)	✅ fixed
S3 requests (GET/PUT)	Requests	PASS	Unit models incompatible ($/1k vs $/10k class ops)	✅ fixed (50x bug)
S3 Intelligent-Tiering monitoring	Objects	PASS	No GCS equivalent (Autoclass is free) — arguably IGNORE	⚠️ change PASS→IGNORE: Autoclass has no monitoring fee
EFS Standard / IA	GB-Mo	SKU	Filestore Basic SSD / HDD	✅ works
FSx Lustre / Windows	GB-Mo	SKU	Filestore High Scale / Enterprise	✅ works
AWS Backup	GB-Mo	PASS	Backup vault ≈ GCS/PD snapshot pricing, needs source-type split	❌ missing — falls to misc/LLM
Databases
Charge type	AWS unit	Decision	GCP target / formula	Status
RDS/Aurora instance-hours	Hrs	SKU	Cloud SQL vCPU+RAM custom (LLM sizes from instance_type)	✅ works
Aurora Serverless v2 ACU	ACU-Hrs	FORMULA	1 ACU = 2 vCPU + 4 GB → Cloud SQL custom SKUs	✅ LLM-guided
RDS storage/backup	GB-Mo	SKU	Cloud SQL storage SKU via block_storage	✅ works
Aurora I/O requests	IOs	PASS	No Cloud SQL I/O charge; folding into storage inflated 1300x	✅ fixed
DynamoDB RCU/WCU/storage	various	PASS (conf ≤ 0.5)	Firestore/Bigtable — workload-dependent, LLM guided	✅ by design (P7-A future)
ElastiCache node-hours	Hrs	FORMULA	Memorystore GiB-hours from node RAM	✅ static mapper
DocumentDB / Neptune / MemoryDB	Hrs	PASS	No managed equivalent / Firestore-ish	✅ service_map
Networking
Charge type	AWS unit	Decision	GCP target / formula	Status
NAT gateway-hours	Hrs	SKU	Cloud NAT gateway-hours	⚠️ split: hours priced ~$3 vs $120 AWS in compare — under-projection; Cloud NAT bills per-VM-hour not per-gateway; needs formula (32-VM assumption or passthrough)
NAT data processed	GB	SKU	Cloud NAT Data Processing $/GB	✅ works ($726/$904 = plausible)
ALB/NLB hours + LCU	Hrs	SKU	Forwarding rules + data processing	✅ works
Internet egress	GB	SKU	Premium Tier internet egress by region	✅ works (excluded from A1 gate)
Inter-AZ / inter-region transfer	GB	SKU	Inter-zone / inter-region egress	✅ works
VPC endpoints	Hrs+GB	PASS	Private Service Connect	✅ service_map
Transit Gateway hours/data	Hrs/GB	SKU	NCC hub + data processing	✅ flat_hourly + data_transfer
Direct Connect port-hours	Hrs	SKU	Dedicated Interconnect	✅ fixed
Route 53 hosted zones/queries	Mo/queries	PASS	Cloud DNS zones/queries — unit-compatible, could be SKU	⚠️ upgrade candidate: zone $0.50 vs $0.20/mo, queries $0.40 vs $0.40/M — trivially mappable
CloudFront egress/requests	GB/Requests	PASS	Cloud CDN — regional rate table needed	✅ new rule (this session)
Analytics & Streaming
Charge type	AWS unit	Decision	GCP target / formula	Status
Redshift node-hours	Hrs	FORMULA	BQ slot-hours via _REDSHIFT_SLOT_MAP	✅ works
Redshift RA3 managed storage	GB-Mo	SKU	BQ Active Storage	✅ works
Athena TB-scanned	TB	SKU	BQ Analysis $/TB	✅ works
EMR instance fee	Hrs	FORMULA	Dataproc Premium $0.01 × vCPU	✅ works
Kinesis shard-hours	Hrs	PASS	Pub/Sub label — reservation vs throughput mismatch	✅ by design
Kinesis data volume	GB/Count	SKU	Pub/Sub Message Delivery	✅ works
Glue DPU-hours	DPU-Hrs	PASS when DPU absent	Dataflow — DPU count rarely in CUR	✅ by design
MSK broker-hours	Hrs	FORMULA	Self-hosted GCE from extracted footprint	✅ LLM-guided
Ops, Security, Messaging
Charge type	AWS unit	Decision	GCP target / formula	Status
CloudWatch log ingest	GB	SKU	Cloud Logging Log Storage	✅ works
CloudWatch DashboardHour	Hrs	IGNORE	Dashboards free on GCP	⚠️ fixed but must read product/operation blob (plan item 4)
CloudWatch metrics/alarms/API	Count	PASS	Cloud Monitoring — alarm pricing incompatible; alarms are free on GCP → candidate IGNORE for alarm rows	⚠️ split alarms → IGNORE, rest PASS
CloudTrail events	Events	PASS	Cloud Audit Logs: first copy free → management-events rows candidate IGNORE	⚠️ split: free-tier rows → IGNORE
X-Ray traces	Traces	SKU	Cloud Trace ingestion (×1.0, ~25x cheaper)	✅ works
KMS key-months	Keys	SKU	Cloud KMS key-versions $1/mo vs $1/mo — exact parity	❌ missing (T7): trivial, high row count
KMS requests	Requests	SKU	Cloud KMS ops $0.03/10k vs $0.03/10k — parity	❌ missing (T7)
Secrets Manager secret-months	Secrets	SKU	Secret Manager $0.06/version vs $0.40/secret	❌ missing — goes to LLM
SNS/SQS requests	Requests	PASS	Pub/Sub throughput-priced vs per-request	✅ service_map
SES emails	Count	PASS	No GCP equivalent	✅ works
GuardDuty / Security Hub	various	PASS	SCC label	✅ works
WAF ACL/rules/requests	Mo/Mo/M	PASS→FORMULA later	Cloud Armor: policy $5/mo + rule $1/mo + $0.60/M much — components map 1:1	⚠️ new rule PASSes now; FORMULA is easy win (HTML report did it correctly)
Shield Advanced	Mo	PASS	Cloud Armor Enterprise $3k/mo parity	✅ new rule
Step Functions transitions	Count	PASS→SKU later	Cloud Workflows steps $0.01/K vs $0.025/K	⚠️ new rule PASSes; SKU upgrade trivial (same unit model)
API Gateway calls	Requests	PASS	Cloud Endpoints label (Apigee rejected — 5.7x)	✅ fixed
Cognito auth requests	Requests	FORMULA	Identity Platform MAU ≈ requests÷20	✅ LLM-guided
Marketplace / Support / Tax / EDP	—	PASS/IGNORE	Non-workload	✅ works
Implementation order for the ⚠️/❌ rows
Fargate rates (blocks the biggest known $3.5k gap) — verify Autopilot SKUs load on next run; if not, add to prefetch_skus.py.
KMS static mapper (T7 — parity pricing, ~46 rows/bill, trivial).
Dashboard/alarm/CloudTrail free-tier IGNORE splits in map_cloudwatch() + CloudTrail rule (kills recurring 3x over-projections).
EKS fee + Public IPv4 + Route 53 PASS→SKU upgrades (exact or near-parity units, mechanical).
WAF + Step Functions FORMULA upgrades (component pricing known from the HTML report's correct treatment).
NAT gateway-hours under-projection (needs a per-VM-hour modeling decision — flag for your review before coding).
Lambda static mapper (T8) + Lambda requests SKU.
S3 IT monitoring PASS→IGNORE, ECS fee PASS→IGNORE (small correctness wins).
io2 provisioned-IOPS rule check (verify it isn't wrongly ignored like gp3).
AWS Backup mapper (needs source-type split — lowest priority).
Store this matrix as data/pricing_matrix.json (service × charge_type → decision + reason + status) so mappers can assert against it and the report can show "decision source: matrix v1" per row — that makes it the auditable per-SKU analysis your manager asked for, not just documentation.

Combined with the 7 code-inconsistency fixes from the previous plan, this is the full work order. Items 1–5 are safe for the lower-grade model; item 6 needs your input on the VM-count assumption.

Don't be just limited to this bill check is the logic really good for each SKU . Is it global can handle all cases with really high mapping accuracy?
Good question — the honest answer requires checking against all 75 job DBs, not reasoning from this bill. Let me run a global audit: for every charge-type pattern across all historical bills, does the current routing/pricing logic handle it?

Bash Audit per-SKU consistency across all job DBs
IN
cd /private/tmp/Codex-501/-Users-arnavchaurasia-Downloads-cur-web-product/563ba0a1-495e-463e-927d-8af9caa50b99/scratchpad && cat > global_sku_audit.py << 'EOF'
#!/usr/bin/env python3
"""Global per-SKU logic audit across ALL job DBs.
For every (product, usage_type-pattern, unit) combo: which mechanic_group did it get,
what strategy/target, and is the outcome consistent + priced?"""
import duckdb, glob, collections, re

JOBS = glob.glob("/Users/arnavchaurasia/.cur-web-product/jobs/*/projection-audit/projection.duckdb")

# key: (svc, unit) -> outcomes
combos = collections.defaultdict(lambda: {
    "spend": 0.0, "rows": 0, "jobs": set(),
    "groups": collections.Counter(), "strategies": collections.Counter(),
    "targets": collections.Counter(), "ratios": [], "null_cost_spend": 0.0,
    "examples": set()})

def svc_key(product):
    p = (product or "").strip()
    p = re.sub(r'^(Amazon|AWS)\s+', '', p)
    p = re.sub(r'\s+(APS\d|USE\d|USW\d|EUC\d|EUW\d|APN\d|APSE\d|EU|US|AP|SA|ME|Global)[-:\s].*$', '', p, flags=re.I)
    return p[:40]

jobs_ok = 0
for db in JOBS:
    try:
        con = duckdb.connect(db, read_only=True)
        tabs = {t[0] for t in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        if "aws_li_to_gcp_li" not in tabs: con.close(); continue
        ccols = {c[0] for c in con.execute("DESCRIBE aws_li_catalog").fetchall()}
        mg = "c.mechanic_group" if "mechanic_group" in ccols else "NULL"
        has_proj = "gcp_projection" in tabs
        q = f"""
        SELECT c.product, c.usage_type, c.pricing_unit, {mg},
               m.strategy, m.gcp_service, c.aws_amortized_cost,
               {'p.gcp_projected_cost' if has_proj else 'NULL'}
        FROM aws_li_to_gcp_li m JOIN aws_li_catalog c USING(aws_li_key)
        {'LEFT JOIN gcp_projection p USING(aws_li_key)' if has_proj else ''}
        WHERE c.aws_amortized_cost > 0
        """
        job = db.split("/jobs/")[1].split("/")[0][:8]
        for prod, ut, unit, grp, strat, tgt, aws, gcp in con.execute(q).fetchall():
            k = (svc_key(prod), (unit or "?"))
            e = combos[k]
            e["spend"] += aws or 0; e["rows"] += 1; e["jobs"].add(job)
            e["groups"][grp or "?"] += 1
            e["strategies"][strat or "?"] += 1
            e["targets"][tgt or "NULL"] += 1
            if strat in ("map","break_down"):
                if gcp is None: e["null_cost_spend"] += aws or 0
                elif aws and aws > 5: e["ratios"].append(gcp/aws)
            if len(e["examples"]) < 2: e["examples"].add((ut or "")[:50])
        jobs_ok += 1; con.close()
    except Exception: pass

print(f"jobs: {jobs_ok}, combos: {len(combos)}")
print("\n=== PROBLEM COMBOS (inconsistent group OR inconsistent target OR bad ratios OR null costs) ===")
print(f"{'service':<38} {'unit':<14} {'jobs':>4} {'spend':>10}  problem")
for (s, unit), e in sorted(combos.items(), key=lambda kv: -kv[1]["spend"]):
    if e["spend"] < 50: continue
    problems = []
    if len(e["groups"]) > 1:
        problems.append(f"GROUPS:{dict(e['groups'].most_common(3))}")
    real_tgts = {t for t in e["targets"] if t != "NULL"}
    if len(real_tgts) > 2:
        problems.append(f"TARGETS:{sorted(real_tgts)[:4]}")
    if e["null_cost_spend"] > 20:
        problems.append(f"NULLCOST:${e['null_cost_spend']:,.0f}")
    if e["ratios"]:
        hi = [r for r in e["ratios"] if r > 3]; lo = [r for r in e["ratios"] if r < 0.2]
        if len(hi) > 2: problems.append(f"OVER3X:{len(hi)}/{len(e['ratios'])}rows max={max(e['ratios']):.0f}x")
        if len(lo) > 2: problems.append(f"UNDER0.2X:{len(lo)}/{len(e['ratios'])}rows")
    if "NULL" in e["targets"] and e["targets"]["NULL"] > e["rows"]*0.3:
        problems.append(f"NULLSVC:{e['targets']['NULL']}/{e['rows']}rows")
    if problems:
        print(f"{s:<38} {unit:<14} {len(e['jobs']):>4} {e['spend']:>10,.0f}  {' | '.join(problems)[:130]}")
EOF
python3 global_sku_audit.py 2>/dev/null | head -80

OUT
jobs: 75, combos: 476

=== PROBLEM COMBOS (inconsistent group OR inconsistent target OR bad ratios OR null costs) ===
service                                unit           jobs      spend  problem
AmazonEC2                              Hrs               3  4,836,025  OVER3X:454/8352rows max=32x | UNDER0.2X:106/8352rows
Fortinet FortiPoints/FortiFlex AWSMPOCB ?                 9  1,782,171  GROUPS:{'non_workload': 7, 'misc': 2}
Elastic Compute Cloud running Linux/UNIX Hrs              30  1,401,033  GROUPS:{'misc': 3428, 'compute_breakdown': 3220} | TARGETS:['Compute Engine', 'Manual Review — Accelerator', 'Manual Sizing Requir
Elastic Compute Cloud                  Hrs              40  1,200,138  GROUPS:{'compute_breakdown': 3735, 'misc': 165, 'flat_hourly': 7} | TARGETS:['Cloud VPN', 'Compute Engine', 'Manual Review — Accel
Relational Database Service            ?                36    311,779  GROUPS:{'managed_db': 175, 'block_storage': 35} | NULLCOST:$5,893 | UNDER0.2X:14/180rows
Elastic Block Store                    GB-Mo            31    214,914  TARGETS:['Cloud Storage', 'Compute Engine', 'Filestore'] | NULLCOST:$21,722 | OVER3X:7/404rows max=5x | UNDER0.2X:6/404rows
Relational Database Service for PostgreS Hrs              19    204,382  NULLCOST:$16,776 | OVER3X:16/494rows max=6x | UNDER0.2X:48/494rows
Savings Plans for Compute usage        Hrs              37    166,656  NULLSVC:89/89rows
Elastic Compute Cloud running Linux/UNIX ?                 9    164,221  OVER3X:12/856rows max=5x | UNDER0.2X:28/856rows
Elastic Compute Cloud                  GB-Mo            28    122,973  GROUPS:{'misc': 149, 'block_storage': 21} | NULLCOST:$4,320
Relational Database Service            Hrs              38    104,629  NULLCOST:$1,210 | UNDER0.2X:24/200rows
Bandwidth                              GB-Mo            30    104,256  NULLCOST:$14,617 | OVER3X:4/129rows max=12x | UNDER0.2X:4/129rows
Compute Savings Plans                  ?                 6     98,208  NULLSVC:6/6rows
Relational Database Service for MariaDB Hrs               9     95,537  NULLCOST:$5,620 | UNDER0.2X:8/128rows
Prisma Cloud (Annual Contract, StateRAMP ?                 9     85,950  GROUPS:{'non_workload': 7, 'misc': 2}
Relational Database Service for MySQL Co Hrs              12     83,599  NULLCOST:$6,752 | UNDER0.2X:20/126rows
Data Transfer                          GB-Mo            65     66,101  NULLCOST:$2,628 | UNDER0.2X:6/114rows
SageMaker RunInstance                  Hrs              15     65,731  TARGETS:['Amazon SageMaker RunInstance', 'Compute Engine', 'Manual Sizing Required', 'Vertex AI'] | NULLCOST:$3,346 | UNDER0.2X:88
Relational Database Service            GB-Mo            36     64,704  GROUPS:{'managed_db': 52, 'block_storage': 46} | NULLCOST:$2,399
Support (Enterprise) Dollar            ?                 9     58,788  GROUPS:{'non_workload': 7, 'misc': 2}
Simple Storage Service                 GB-Mo            58     57,086  GROUPS:{'object_storage': 276, 'misc': 66, 'per_request': 1} | TARGETS:['Amazon Simple Storage Service', 'Cloud Monitoring', 'Clou
OpenSearch Service                     ?                36     51,130  TARGETS:['Compute Engine', 'Manual Review', 'OpenSearch Service'] | UNDER0.2X:32/76rows
Redshift Node Usage Reserved Instances Hrs              15     44,922  GROUPS:{'misc': 19, 'redshift': 8}
Elastic Load Balancing - Application   Hrs              30     43,093  GROUPS:{'flat_hourly': 48, 'misc': 40} | TARGETS:['Elastic Load Balancing - Application', 'Networking', 'Vertex AI'] | NULLCOST:$4
Database Migration Service CreateDMSInst Hrs              15     42,549  GROUPS:{'compute_breakdown': 78, 'misc': 43} | TARGETS:['Compute Engine', 'Database Migration', 'Database Migration Service'] | UN
Elastic Block Store                    ?                28     41,786  UNDER0.2X:4/34rows
Managed Streaming for Apache Kafka     Hrs              36     40,596  TARGETS:['Cloud Pub/Sub', 'Compute Engine', 'Managed Service for Kafka', 'Managed Streaming for Apache Kafka'] | UNDER0.2X:34/68ro
Elastic Compute Cloud NatGateway       GB-Mo            25     34,503  NULLCOST:$578
Elastic Compute Cloud running Ubuntu Pro Hrs               9     33,634  NULLCOST:$4,071
ElastiCache                            Hrs              39     32,216  GROUPS:{'managed_db': 189, 'elasticache': 14} | TARGETS:['Cloud Memorystore for Memcached', 'Cloud Memorystore for Redis', 'Memory
Redshift Node Usage                    Hrs              18     28,712  GROUPS:{'misc': 25, 'redshift': 8} | UNDER0.2X:4/5rows
GuardDuty                              ?                34     25,115  GROUPS:{'misc': 204, 'guardduty': 122}
Redshift                               ?                 6     24,867  GROUPS:{'per_request': 4, 'misc': 4, 'redshift': 1}
CloudTrail                             ?                26     24,438  GROUPS:{'misc': 396, 'per_request': 20} | TARGETS:['AWS CloudTrail APN1-PaidEventsRecorded', 'AWS CloudTrail APN2-PaidEventsRecord
Elastic Compute Cloud running Windows  Hrs              15     23,736  NULLCOST:$2,212 | UNDER0.2X:22/194rows
Virtual Private Cloud                  Hrs              40     23,695  GROUPS:{'misc': 108, 'flat_hourly': 72} | TARGETS:['Compute Engine', 'Networking', 'VPC Network'] | NULLCOST:$219
Virtual Private Cloud TransitGatewayVPC Hrs              18     23,386  GROUPS:{'flat_hourly': 26, 'misc': 10}
AmazonCloudWatch PutLogEvents          GB-Mo            21     22,168  GROUPS:{'cloudwatch': 61, 'misc': 17} | NULLCOST:$2,742
SageMaker RunInstance                  ?                14     21,233  TARGETS:['Amazon SageMaker RunInstance', 'Compute Engine', 'Vertex AI'] | UNDER0.2X:4/9rows
Relational Database Service for Aurora M Hrs               9     17,277  NULLCOST:$1,016 | UNDER0.2X:6/128rows
Elastic Compute Cloud running Red Hat En Hrs              15     16,881  GROUPS:{'misc': 120, 'compute_breakdown': 50} | NULLCOST:$1,770 | UNDER0.2X:14/151rows
Simple Storage Service                 ?                43     16,492  GROUPS:{'misc': 197, 'per_request': 146} | TARGETS:['Amazon Simple Storage Service', 'Amazon Simple Storage Service APS1-Requests-
CloudFront IN-Requests-HTTPS-Proxy     ?                 6     16,075  TARGETS:['Amazon CloudFront IN-Requests-HTTPS-Proxy', 'Compute Engine', 'Networking'] | NULLCOST:$2,679
CloudWatch                             ?                43     15,913  GROUPS:{'cloudwatch': 138, 'misc': 105} | TARGETS:['Cloud Logging', 'Cloud Monitoring', 'Cloud Monitoring + Logging'] | UNDER0.2X:
GuardDuty                              GB-Mo            47     15,839  GROUPS:{'guardduty': 38, 'misc': 21, 'object_storage': 9} | TARGETS:['Cloud Logging', 'GuardDuty', 'Security Command Center']
Elastic Container Service for Kubernetes Hrs              26     14,572  GROUPS:{'misc': 55, 'non_workload': 7} | TARGETS:['Cloud Run', 'GKE Autopilot', 'Kubernetes Engine']
OpenSearch Service                     Hrs              26     14,428  GROUPS:{'compute_breakdown': 58, 'misc': 38} | TARGETS:['Compute Engine', 'Manual Review', 'OpenSearch Service']
ElastiCache for Redis                  Hrs              14     13,374  GROUPS:{'managed_db': 62, 'elasticache': 8} | NULLCOST:$607
Virtual Private Cloud                  GB-Mo            15      9,842  GROUPS:{'object_storage': 22, 'misc': 6} | TARGETS:['Cloud Storage', 'Compute Engine', 'VPC Network'] | NULLCOST:$2,001
Elastic Container Service              Hrs              11      9,760  GROUPS:{'misc': 18, 'per_request': 8} | NULLCOST:$3,518
Elastic Load Balancing                 Hrs              41      9,593  GROUPS:{'flat_hourly': 74, 'misc': 26} | TARGETS:['Cloud Load Balancing', 'Compute Engine', 'Networking'] | NULLCOST:$365
Route 53                               ?                41      9,304  TARGETS:['Cloud DNS', 'Cloud Monitoring', 'Compute Engine', 'Networking'] | NULLCOST:$650 | UNDER0.2X:21/83rows
QuickSight                             ?                20      8,925  GROUPS:{'misc': 44, 'per_request': 14}
Lambda                                 GB-Mo            18      8,453  GROUPS:{'object_storage': 66, 'misc': 3} | TARGETS:['Cloud Run', 'Cloud Run Functions', 'Cloud Storage'] | OVER3X:4/22rows max=61x
Simple Storage Service TimedStorage-Byte GB-Mo            25      7,744  GROUPS:{'object_storage': 28, 'misc': 6} | NULLCOST:$860
ElastiCache for Memcached              Hrs              12      7,370  NULLCOST:$455 | UNDER0.2X:8/34rows
Elastic Compute Cloud NatGateway       Hrs              25      6,969  NULLCOST:$600 | UNDER0.2X:15/22rows
OpenSearch Service ESDomain            ?                 4      6,598  UNDER0.2X:6/12rows
Virtual Private Cloud TransitGatewayVPN Hrs              18      6,584  GROUPS:{'flat_hourly': 26, 'misc': 10}
Shield Shield-Monthly-Fee              ?                 6      6,000  TARGETS:['AWS Shield Shield-Monthly-Fee', 'Cloud Armor', 'Networking']
Elastic Compute Cloud running Windows Re Hrs               9      5,957  NULLCOST:$596
MemoryDB CreateCluster                 Hrs               9      5,671  TARGETS:['Cloud Memorystore for Memcached', 'Cloud Memorystore for Redis', 'Cloud SQL'] | NULLCOST:$270
OpenSearch Service ESDomain            Hrs              13      5,546  GROUPS:{'compute_breakdown': 27, 'misc': 20}
Managed Streaming for Apache Kafka RunBr Hrs               4      5,504  UNDER0.2X:6/12rows
Elastic Load Balancing - Network       Hrs              26      5,476  GROUPS:{'flat_hourly': 26, 'misc': 18} | TARGETS:['Compute Engine', 'Networking', 'Vertex AI'] | NULLCOST:$556 | UNDER0.2X:4/41row
Elastic Compute Cloud                  ?                26      5,320  NULLCOST:$201 | UNDER0.2X:3/53rows
OCBCloudFront UE-OCB                   ?                 6      5,001  TARGETS:['Compute Engine', 'Networking', 'OCBCloudFront UE-OCB', 'passthrough'] | NULLCOST:$834
OpenSearch Service                     GB-Mo            26      4,740  GROUPS:{'block_storage': 26, 'misc': 26} | TARGETS:['Compute Engine', 'Manual Review', 'OpenSearch Service']
Relational Database Service for Aurora P Hrs               9      4,279  NULLCOST:$130 | UNDER0.2X:6/32rows
Elastic File System Provisioned Throughp ?                15      4,017  GROUPS:{'efs': 10, 'misc': 5} | TARGETS:['Cloud Filestore', 'Cloud Storage', 'Elastic File System (EFS)', 'Filestore']
ElastiCache for Redis Reserved Instances Hrs               8      3,806  NULLCOST:$1,384
Transfer Family SFTP:S3                Hrs              14      3,696  GROUPS:{'misc': 10, 'per_request': 4} | TARGETS:['AWS Transfer Family SFTP:S3', 'Cloud Storage', 'Compute Engine']
SageMaker CreateVolume-Gp3             GB-Mo            15      2,740  TARGETS:['Amazon SageMaker CreateVolume-Gp3', 'Compute Engine', 'Vertex AI'] | NULLCOST:$375
WAF                                    ?                16      2,546  GROUPS:{'misc': 41, 'per_request': 5} | TARGETS:['AWS WAF APS3-RequestV2-Tier1', 'AWS WAF USE1-RequestV2-Tier1', 'AWS WAF USE1-Req
Elastic MapReduce                      Hrs              15      2,434  GROUPS:{'misc': 46, 'per_request': 37, 'emr': 11} | TARGETS:['Cloud Storage', 'Compute Engine', 'Dataproc']
Elastic Compute Cloud                  GB                3      2,250  GROUPS:{'misc': 12, 'data_transfer': 5}
Shell cwd was reset to /Users/arnavchaurasia/Downloads/cur-web-product
No — the data says it isn't yet. I aggregated all 75 jobs (476 distinct service×unit combos) and checked every combo for four failure signals: inconsistent mechanic_group across runs, inconsistent GCP target, NULL projected cost on mapped rows, and ratio anomalies. One caveat before the findings: the aggregate includes runs from before recent fixes, so some inconsistency is historical noise — but many gaps are provably still in today's code. Here's the per-SKU verdict on everything the audit flagged.

Confirmed gaps in today's code (not historical noise)
1. SageMaker — completely unhandled, ~$90K spend across 15 jobs. No classifier rule, no service_map entry. Targets observed: the echo bug ("Amazon SageMaker RunInstance" as gcp_service), Compute Engine, "Manual Sizing Required", and Vertex AI — four different answers for the same SKU. Fix: sagemaker rules — RunInstance Hrs → Vertex AI training/prediction (review/passthrough), CreateVolume-Gp3 → passthrough with Vertex AI label, notebook instances → Vertex AI Workbench.

2. OS-licensed EC2 (Windows, RHEL, Ubuntu Pro) — systematic under-projection. "EC2 running Windows" shows 22 of 194 rows under 0.2x; RHEL and Ubuntu Pro rows cluster the same way, plus routing splits (RHEL: 120 misc vs 50 compute_breakdown). Root cause: the AWS hourly price includes the OS license premium, but we map to Linux-rate core+ram SKUs. GCP bills licenses separately (Windows Server ~$0.046/vCPU-hr, RHEL per-instance tiers). Fix: compute_breakdown must catch "running Windows/RHEL/Ubuntu Pro" product variants, and family_mapper must emit a third license component using GCP premium-image SKUs. This is the largest accuracy gap in the core compute path — the thing we price best everywhere else.

3. Transfer Family (SFTP) — no rule. Routes randomly (misc vs per_request), targets include the echo string. GCP has no managed SFTP; correct decision is passthrough with "no equivalent — self-host or Storage Transfer Service" note.

4. Database Migration Service — inconsistent and mispriced. 78 rows land in compute_breakdown (DMS instance-hours look like EC2), 43 in misc; targets split three ways. GCP's DMS is free for homogeneous migrations — mapping DMS instance-hours to GCE compute over-projects. Fix: classifier rule before compute_breakdown → review/ignore decision per row type.

5. Route 53 core (zones/queries) — missing from service_map. Only "route 53 resolver" exists, so plain Route 53 rows got Cloud Monitoring, Compute Engine, and Networking as targets across runs, with 21 of 83 rows under 0.2x. Fix: add "route 53" → Cloud DNS rule (order it after the resolver rule).

6. MemoryDB → observed mapped to Cloud SQL. Wrong target — MemoryDB is Redis-compatible; the right target is Memorystore for Redis. The managed_db classifier catches MemoryDB product names and hands it to the LLM as a database. Fix: route MemoryDB to the elasticache static mapper instead.

7. Lambda ephemeral-storage rows (GB-Mo) misrouted to object_storage → 61x max over-projection. 66 rows across 18 jobs mapped to Cloud Storage. Verify the current rule chain on a fresh run — if block_storage's generic "Storage" regex or S3 fallthrough still catches Lambda GB-Mo rows, add Lambda product exclusion (same class of bug as the EFS/FSx ordering fix).

8. VPC endpoint GB-Mo rows → object_storage misroute ($2K NULL cost; target shows Cloud Storage). Same rule-ordering class: "Storage" regex catching non-storage rows. The block_storage generic rule needs a product exclusion list extension (VPC, Lambda).

9. Savings-Plan-covered usage — 89 of 89 rows have NULL gcp_service at $167K spend. These are commitment rows and may be intentionally NULL, but my new null_gcp_service gate will now fail every bill containing them unless they're strategy='ignore' or is_workload=false. The implementation model must verify apply_commitment_ignores.py stamps these correctly before the gate lands, or the gate needs AND m.strategy NOT IN ('ignore') plus an is_workload filter (it has the workload filter; verify commitment rows actually carry is_workload=false in all schema versions).

10. Marketplace resellers (Fortinet $1.8M, Prisma $86K) leak to misc in 2 of 9 jobs. The non_workload rule matches "AWSMP" but some OCB (offline channel billing) product strings apparently miss all patterns. Add "OCB" and "FortiFlex"-style patterns, and make the normalizer's marketplace canonical key the routing signal once Layer 1 is consumed by the classifier.

11. The echo bug is widespread and historical runs prove Layer 4 is necessary: observed gcp_service values include "AWS WAF APS3-RequestV2-Tier1", "AWS Shield Shield-Monthly-Fee", "Amazon CloudFront IN-Requests-HTTPS-Proxy", "Elastic Load Balancing - Application", and even "Vertex AI" on an ELB row (pure hallucination). The echo gate catches the AWS-prefix class but not the hallucinated-GCP-name class ("Vertex AI" on a load balancer). That's why Layer 2 must be a closed enum with per-service allowed-target lists — gcp_service must be one of the targets service_map allows for that canonical_service, not merely "a GCP-sounding name". Extend the Layer 2 design: service_map.json gains "allowed_targets": [...] per rule, and a post-merge gate rejects any row whose target isn't in its service's list.

12. The SP/RI-coverage ratio problem shows up at scale: the $4.8M EC2 job has 454 rows over 3x (max 32x) — SP-discounted amortized AWS cost compared against list-price GCP OD. Under the 50x outlier gate, invisible. This is the netting-policy decision from earlier, now confirmed as the dominant error source on large enterprise bills. It needs your call: compare GCP OD against AWS list (and show CUD columns against amortized), or net SP into both sides. No code should be written until you decide.

Verdict per layer
The four layers are the right skeleton, but two need strengthening based on this data: Layer 2 needs per-service allowed-target lists (not just a global enum) to kill hallucinated targets, and Layer 1's canonical key must actually be consumed by classify_mechanics (replacing the per-rule _ilike product sniffing) — otherwise items 3, 4, 6, 10 get fixed as yet more one-off aliases, which is exactly the whack-a-mole pattern you're trying to end.

Additions to the implementation plan (priority order)
OS license component for Windows/RHEL/Ubuntu Pro EC2 (biggest dollar-accuracy gap in the best-covered path)
SageMaker rules (biggest unhandled service by spend)
Layer 2 upgrade: per-service allowed_targets + post-merge enforcement gate
Classifier consumes canonical_service (Layer 1 completion) — DMS, MemoryDB, Transfer Family, Route 53, marketplace-OCB all become one-line dictionary entries instead of new regex rules
Lambda/VPC storage-regex exclusions (misroute class)
Commitment-row NULL-service verification before the null_gcp_service gate ships
SP netting policy — decision needed from you, then implement
Want me to fold these into the existing plan document as the final work order for the implementation model?
---

# IMPLEMENTATION STATUS — 2026-07-15 (skeptical review pass, all changes verified: 70/70 tests pass)

## Implemented ✅

| Plan item | Change | File |
|---|---|---|
| A-1/A-6: autofix-after-gate ordering | Both Layer-4 autofixes (map-no-rate downgrade, echo-clear) moved into the AUTOFIX section before all gates; gates are now pure post-fix checks | `scripts/validate_fix.py` |
| A-2: alias substring order | Fallback loop now longest-alias-first; aliases ≤4 chars require word boundaries (ECR no longer normalizes to ec2) | `scripts/aws_normalizer.py` |
| A-3: dead dict key | "amazongUardduty" → "amazonguardduty" | `scripts/aws_normalizer.py` |
| A-4: dashboard PDF blob | Dashboard detection checks product+usage_type+operation (PDF bills have empty usage_type); Fargate-in-suffix check added to normalizer (raw product checked before suffix strip → 'fargate' not 'ecs') | `scripts/apply_static_mappings.py`, `scripts/aws_normalizer.py` |
| A-5: Phase-1 PostLLMScripts | VERIFIED: orchestrator's run loop always runs post_llm_scripts, including script-only phases (orchestrate.go ~line 998: "Deterministic post-LLM scripts always run"). Normalizer hook placement valid. | no change needed |
| A-7: cert manager mode | ignore → review (Private CA $400/mo no longer hidden at $0) | `data/service_map.json` |
| C-2: SageMaker | "sagemaker" → Vertex AI (review) — kills 4-way target variance | `data/service_map.json` |
| C-3: Transfer Family | "transfer family" → Manual Review passthrough | `data/service_map.json` |
| C-5: Route 53 core | "route 53"/"route53" → Cloud DNS (review), ordered AFTER resolver rules | `data/service_map.json` |
| C-6: MemoryDB | "memorydb" → Memorystore (review) — was misrouting to Cloud SQL | `data/service_map.json` |
| C-7/C-8: storage misroutes | Lambda / Virtual Private Cloud / SageMaker excluded from block_storage generic "Storage" regex (Lambda 61x inflation class) | `scripts/classify_mechanics.py` |
| C-9: commitment rows | VERIFIED: apply_commitment_ignores.py sets strategy='ignore' → null_gcp_service gate (which excludes ignore) is safe | no change needed |
| Stale tests | MOCK_SKU now a SKUMeta (resolve_sku returns SKUMeta, not str); _with_sku unpacks (mapped, llm_rows) tuples; "Cloud Dataproc"→"Dataproc" | `tests/test_static_mappers.py` |

## Rejected on skeptical review ❌ (do not re-implement without new evidence)

- **KMS static mapper (T7)**: service_map "key management" rule already forces Cloud KMS passthrough post-merge; KMS pricing is near-parity so passthrough ≈ correct dollars. Observed KMS→Cloud Run misroutes predate service_map.
- **PASS→SKU upgrades (EKS fee, Public IPv4, Route 53)**: parity passthrough already yields identical dollars; SKU resolution only adds failure modes. Zero accuracy delta.
- **Layer-2 allowed_targets machinery**: observed target drift came from MISSING service_map rules (now added), not from enforcement gaps. Revisit only if drift recurs on services that HAVE rules.
- **DMS reroute**: service_map "database migration" rule already forces the target post-merge. Historical inconsistency predates it.
- **Marketplace OCB patterns**: "AWSMPOCB" already matches the existing "AWSMP" pattern; the 2 misc-routed jobs predate it. "OCBCloudFront" now caught by the new cloudfront rule.

## Deferred (needs design or user decision) ⏸

1. **OS license component (Windows/RHEL/Ubuntu Pro)** — license SKUs confirmed present in catalog.duckdb ("Licensing Fee for Windows Server ... (CPU cost)", RHEL vCPU tiers). BUT: blindly rerouting these rows to compute_breakdown WITHOUT a license component makes accuracy WORSE than today's honest LLM passthrough (maps license-inclusive AWS price to Linux-only GCP SKUs). Needs family_mapper design: third `license` component keyed by OS + vCPU tier.
2. **SP/RI netting policy** — dominant error source on large enterprise bills ($4.8M job: 454 rows >3x). USER DECISION REQUIRED: compare GCP OD vs AWS list, or net SP into both sides.
3. **NAT gateway-hours** — Cloud NAT bills per-VM-hour, not per-gateway; needs VM-count assumption. USER DECISION REQUIRED.
4. **classify_mechanics consumes canonical_service** — Layer 1 column is stamped (aws_normalizer.py, hooked as Phase-1 post-script) but classifier rules still use per-rule _ilike. Right long-term refactor; too invasive for a patch pass.
5. **Fargate Autopilot rates** — mapper fixed (prior session); rates self-load next run via incremental_rerate once gcp_sku_id is non-NULL. If NULL costs persist after one run, add Autopilot SKUs to prefetch_skus.py.
