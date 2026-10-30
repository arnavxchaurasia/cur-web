# DESIGN — Service Modelling: the typed-resolution target

**Purpose.** This is the *theory* reference that stops charge-type misrouting at
the design level instead of per-bug. It explains **why** classification must be
typed resolution rather than string matching, states the invariants that make the
mapping complete (not whack-a-mole), and gives a repeatable method for modelling
**any** AWS service so the next addition is a data edit, not a new regex.

It complements, and does not replace:
- [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) — the end-to-end pipeline map (Layers 0–4).
- [SERVICE_SPECS_DESIGN.md](SERVICE_SPECS_DESIGN.md) — the `service_specs.json` schema + migration.
- [WORKORDER_spec_dispatch_cutover.md](WORKORDER_spec_dispatch_cutover.md) — the wiring task that makes this real.

---

## 1. The core defect regex can never fix

The legacy classifier answers two different questions with one flat,
first-substring-wins scan:

1. **Identity** — *what service is this row?*
2. **Classification** — *given the service, what charge-type is it and how is it priced?*

Resolving both by "first `ILIKE '%pattern%'` across all services wins" produces two
failures that are **structural, not tunable**:

- **Overlap.** `%Storage%` matches Lambda, VPC, S3, EBS. One pattern claims rows
  from many services. (This is why `block_storage` carries 5 hard-coded
  exclusions — S3, DynamoDB, Lambda, VPC, SageMaker. Each exclusion is a scar.)
- **Order-dependence.** `ec2` matching before `ec2 container registry`. Precedence
  comes from a rule's *position in the file*, which is unrelated to correctness.

### The formal framing
Correct classification is a **total, disjoint partition** of rows: every row lands
in exactly one bucket, and no two rules can both claim it. Greedy substring
scanning over a flat string is a **partial, overlapping function** — the wrong
mathematical object. You cannot tune your way from "overlapping" to "partition";
you must change the structure. Regex's only legitimate job here is **parsing a
string into typed fields**, never **deciding** a mechanic.

---

## 2. The four modelling moves (how FinOps tooling does it)

### Move 1 — Identity from a stable ID, never a display name
Resolve `product_code → canonical_service` as a **dictionary lookup**. AWS renames
marketing strings; `ProductCode`/`servicecode` are stable machine IDs.
**Status: DONE** (`aws_normalizer.py`, product_code-first, word-boundary guarded).
Display-name matching survives only as a last-resort fallback for rows missing a
code.

### Move 2 — A bill line is a typed tuple, not a string
Model each row as a record of typed dimensions:
```
(product_code, usage_type, operation, pricing_unit, region, line_item_type, cost…)
```
`usage_type` itself has a grammar — `APS3-Lambda-Storage-Duration-GB-Mo`
parses to `(region=APS3, family=Lambda, subtype=Storage-Duration, unit=GB-Mo)`.
**Classify on the parsed struct, not on raw substrings.** Once `family=Lambda` is a
field, the word "Storage" inside it is inert — the Lambda/EBS collision cannot
occur. (Pieces exist: `region_prefix.py`, `unit_equivalence.py`, `schema_mapper.py`;
the gap is formalizing one typed `parse(usage_type, operation) -> fields`.)
**PDF/flat-bill rule:** the signal may live in `operation`/`product` when
`usage_type` is empty — the parser must read a *blob across fields* with
provenance of which field supplied each token (the DashboardHour bug).

### Move 3 — Classify *within* a service, by specificity
After identity, consult **only that service's** charge-types. Precedence inside a
service is **most-specific-match-wins** (most-constrained predicate, or an explicit
priority int) — never file order. This is a scoped decision table, like a routing
table or tax code: bounded, ordered, and *isolated* — a Lambda charge-type
physically cannot see an EBS row, so cross-service ordering fragility is gone.
**Status: BUILT, not wired** (`service_specs.json` = 54 services; `spec_dispatch.py`
in shadow mode). The cutover work order flips it on.

### Move 4 — Close the world with typed contracts + a golden corpus
- **Closed `allowed_targets`.** Each charge-type declares the GCP services it may
  map to. A target outside the set is **rejected by a gate**, not accepted as "a
  GCP-sounding name." This kills the hallucination class (`Vertex AI` on an ELB
  row) by construction — the string isn't in the allowed set.
- **Unit contracts.** Each charge-type declares its unit dimension
  (`GB-Mo`, `vCPU-hr`, `requests`); the converter asserts dimensional
  compatibility before pricing. A `GB-Mo` row can never reach an hourly mechanic —
  the types don't unify. (Foundation: `service_contracts.py`, `unit_equivalence.py`.)
- **Golden corpus.** A frozen `(service, usage_type) → expected mechanic` table,
  regression-tested on every change (`verify_golden_mappings.py`). This is how a
  fix is *proven* not to break a sibling — the guarantee regex edits never give.

---

## 3. The invariants (true at every commit, for every service)

1. **Total.** No row exits classification with `mechanic_group = NULL`. Unmatched
   rows hit an explicit, counted generic fallback — never silence.
2. **Disjoint.** No row is claimed by two charge-types within a service
   (`spec_parity.py` `ambiguous` must be 0 before cutover) and never by two
   services (guaranteed by identity-before-classification).
3. **Typed.** Every classification decision reads parsed fields, not raw
   substrings. Pricing reads a declared unit dimension, not an inferred one.
4. **Closed.** Every mapped target is in its charge-type's `allowed_targets`.
   Out-of-enum = gate failure.
5. **Provable.** Every (service, usage_type) in the golden corpus maps to its
   expected mechanic; CI fails otherwise.
6. **Auditable.** Each row carries `canonical_service`, `charge_type`,
   `field_provenance`, `od_basis`, and `decision_source` so the report explains
   every number.

---

## 4. Method — modelling ANY new service (the repeatable recipe)

Adding a service becomes a **data edit + a golden test**, never a code regex:

1. **Identify.** Add its `product_codes` to the canonical map (confirm with
   `aws_normalizer`). Identity first.
2. **Enumerate charge-types.** List the distinct charge-types the service bills
   (e.g. KMS → `key-months`, `requests`). For each, write the `match` predicate
   over *parsed fields*, ordered most-specific-first, `ambiguous`=0.
3. **Assign a mechanic.** Point each charge-type at a shared pricing primitive
   (compute / block_storage / data_transfer / per_request / flat_hourly / …) or a
   terminal decision (SKU / FORMULA / PASS / IGNORE per the Layer-3 taxonomy).
   **Prefer FORMULA/SKU over PASS whenever the unit model is compatible** — PASS
   just launders the AWS number through a GCP label; it is not a projection.
4. **Declare contracts.** Set `allowed_targets` and the unit dimension for each
   charge-type.
5. **Prove it.** Add golden rows `(service, real usage_type) → expected mechanic`.
   Run `spec_parity.py`; drive `differ`/`spec_gap`/`ambiguous` to target. Any
   intentional `differ` (spec fixes a legacy bug) is logged, not hidden.
6. **Let the loop maintain it.** New/uncertain rows flow to the advisory reviewer;
   accepted findings persist to the refresh store and short-circuit the LLM next
   run (see [WORKORDER_reviewer_refresh_loop.md](WORKORDER_reviewer_refresh_loop.md)).

If step 2 or 3 is genuinely ambiguous for a high-$ service, **escalate a structured
finding** — do not encode a guess as a rule. An honest passthrough beats a
confident wrong mechanic.

---

## 5. Where this leaves the architecture

The modelling is **not missing — it is ~85% built and partly unwired**:

| Move / invariant | State | Lever |
|---|---|---|
| Identity from product_code | ✅ done | — |
| Typed usage_type parse | ⚠️ pieces exist | formalize one `parse()` |
| Scoped per-service dispatch | ⚠️ built, shadow-only | [cutover work order](WORKORDER_spec_dispatch_cutover.md) |
| `allowed_targets` enum gate | ⚠️ partial | enforce as Layer-4 gate |
| Unit contracts | ⚠️ partial | assert in converter |
| Golden corpus | ⚠️ exists, grow it | add rows per cutover |
| Self-healing loop | ⚠️ scaffolded | [reviewer/refresh work order](WORKORDER_reviewer_refresh_loop.md) |

**Bottom line.** Stop deciding on strings; resolve typed keys against scoped
tables and gate the output against closed enums and unit contracts. Regex is
demoted to parsing at the edge. Misrouting then becomes a *type error a gate
catches loudly*, not a substring that silently slipped through — which is the
difference between a mapping that is complete and one that is forever patched.
