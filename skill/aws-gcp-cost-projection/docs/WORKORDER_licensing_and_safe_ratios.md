# WORK ORDER — Finish OS/licence modelling, and make assumption ratios tamper-safe

**For an implementing AI with no prior context. Read §0 and §1 first.**

Four items. **Item 4 is a safety fix — an admin can currently produce an absurd
projection while staying inside every validation bound.** Do it first.

Nothing here changes a GCP rate or a mapping rule. If you find yourself editing
a SKU choice or a price, stop.

Baselines: **all Go packages passing, 1,130 Python tests, `tsc -b` clean.**

---

## 0. What already works — do not rebuild it

OS licence modelling is **already implemented on both sides**. Read these
before touching anything:

| side | where | what it does |
|---|---|---|
| AWS | `scripts/license_split.py` | splits the licence-inclusive AWS on-demand cost into `instance + licence` using `license_fraction` |
| AWS data | `scripts/refresh_ec2_specs.py` | computes `license_fraction` per instance type from the real AWS Price List Windows/Linux ratio |
| GCP | `scripts/apply_static_mappings.py` | emits a real `license` component: Windows (flat per-VM SKU), RHEL / Ubuntu Pro / SUSE (vCPU-banded SKUs), SQL Server (separate component) |

`license_fraction` is present for **1,250 of 1,315** instance types. This work
order is about the remainder and about one safety hole — **not** about
rebuilding the above.

---

## 1. Rules

1. **Never fabricate a price input.** If AWS does not publish a licence ratio
   for a shape, the answer is "state that we cannot split it", not a guess.
2. **A guess that cannot be distinguished from a measurement is a defect.**
   Every assumed value must be visible as assumed.
3. **Bounds must constrain what actually matters.** A value inside its own
   range can still be nonsense in combination — see Item 4.
4. **Default closed.** On invalid input, fall back to the known-safe default
   and say so.

---

## ITEM 4 (DO FIRST) — Per-value bounds do not constrain the ratio

### The problem

Assumptions are validated individually:

```
aurora_acu_vcpu      default 2.0    min 0.1   max 4.0
aurora_acu_ram_gib   default 16.0   min 0.5   max 64.0
```

Each bound is sensible alone. But the thing that matters is the **ratio** —
1 ACU ≈ 2 vCPU + 16 GiB is a **1:8** vCPU:RAM shape, which is what makes it a
memory-optimised machine on the GCP side.

An admin can set `aurora_acu_vcpu = 0.1` and `aurora_acu_ram_gib = 64` — both
inside bounds — producing a **1:640** shape. That is not a machine that exists.
Every Aurora Serverless row in every later projection is then wrong, and
nothing warns anyone.

The same applies to `glue_dpu_vcpu` / `glue_dpu_ram_gib` (1:4 today, bounds
allow 1:256).

### The fix

**1. Declare the relationship, not just the values.**

Add a ratio constraint alongside the existing per-value bounds. Store it as
data, next to the assumptions seed in `internal/db/db.go`:

```
ratio_group: "aurora_acu"   numerator: aurora_acu_ram_gib
                            denominator: aurora_acu_vcpu
                            min_ratio: 2.0    max_ratio: 16.0
                            default_ratio: 8.0

ratio_group: "glue_dpu"     numerator: glue_dpu_ram_gib
                            denominator: glue_dpu_vcpu
                            min_ratio: 2.0    max_ratio: 8.0
                            default_ratio: 4.0
```

Those bands are the real machine shapes: GCP machine families run roughly 1:1
(compute-optimised) to 1:8 (memory-optimised), with 1:16 only on extreme-memory
types. **A ratio outside 1:2–1:16 is not a machine you can buy**, which is why
the band is the right thing to enforce.

**2. Validate on write.** When either member of a ratio group changes,
recompute the resulting ratio and **reject the change** if it falls outside the
band, with a message naming both values and the resulting ratio:

> "aurora_acu_vcpu = 0.1 with aurora_acu_ram_gib = 64 gives a 1:640
> vCPU:RAM shape. Allowed range is 1:2 to 1:16."

**3. Make "reset to defaults" authoritative.** There is already a
`default_value` column and a reset endpoint — verify reset restores **every**
assumption to `default_value`, and add a test. The defaults are your safe
fallback; they must not be editable through the API.

**4. Defend at read time too.** In `config_loader.load_assumptions`, after
loading, check each ratio group. If a stored ratio is outside its band (set
before this validation existed, or edited directly in the DB), **use the
default and emit a `diag(WARN, "assumption_ratio_out_of_band", ...)`**. A bad
value must never silently reach a customer's projection.

### Tests
1. setting `aurora_acu_vcpu = 0.1` while ram is 64 → rejected, both values
   unchanged
2. setting a ratio inside the band → accepted
3. reset restores every assumption to `default_value`
4. `load_assumptions` with an out-of-band stored pair returns the **defaults**
   and emits the diagnostic
5. the per-value min/max checks still work (do not replace them — this is an
   additional layer)

---

## ITEM 1 — `.metal` instances are mis-sized and unlicensed

### What I found

65 instance types lack `license_fraction`. They are **almost entirely
`.metal`**: `a1.metal`, `c5.metal`, `c6g.metal`, `c7i.metal-24xl`,
`c7a.metal-48xl`, plus `trn2`.

That absence is **correct** — AWS does not publish a comparable
Windows/Linux on-demand pair for bare metal in the same form. **Do not
fabricate a fraction for them.**

But there is a real bug next to it. `apply_static_mappings.py:7241`:

```python
"48xlarge": 192, "metal": 64,
```

Every `.metal` shape is sized at a flat **64 vCPU**. That is wrong for most of
them — `c5.metal` is 96, `m5.metal` is 96, `x2iedn.metal` is 128. So metal rows
are silently mis-sized, and the error is invisible.

### The fix

**1. Size metal from the specs file, not a constant.** `ec2-instance-types.json`
has real `vcpus` and `ram_gb` for metal types (that is how I know `c5.metal` is
96). Look the type up; use the constant only when the lookup fails, and emit a
diagnostic when it does.

**2. Decide what a metal instance maps to.** GCP has no drop-in bare-metal
Compute Engine shape. The honest options, in order of preference:

- **Sole-tenant node** — the closest real equivalent; price the node, note that
  tenancy differs
- **Passthrough** with `passthrough_reason = "no_gcp_equivalent"` and a note
  saying bare metal has no direct GCP VM equivalent

Pick one, implement it, and **state the choice in the projection note**. Do not
let metal rows flow through the ordinary VM path as though they were ordinary
VMs — that is what happens today.

**3. Licence on metal:** with no `license_fraction`, do **not** split the AWS
cost and do **not** emit a GCP licence component. Add a note: *"Bare-metal
licence terms are not modelled; AWS cost shown inclusive."* Visible, not
silent.

### Tests
1. `c5.metal` resolves to 96 vCPU, not 64
2. a metal row carries the chosen strategy and a note naming it
3. a metal row produces no fabricated licence split

---

## ITEM 2 — Accelerator instances (`trn2`, `inf*`) licence handling

`trn2` appears in the missing list for the same reason: no Windows/Linux pair.

These are already handled on the compute side (Inferentia/Trainium → Cloud TPU
as a labelled assumption). The only gap is that they must **not** receive a
fabricated licence split either.

### Fix
Same as Item 1.3 — no split, no GCP licence component, and a note. Confirm the
existing TPU mapping note already discloses the 1:1 chip assumption; if it does
not, add it.

---

## ITEM 3 — Oracle is modellable for License Included rows; today it is not

### What I found

AWS populates a `license_model` column and the pipeline **already captures
it** — across the corpus, 917 rows read `License Included` and 11 are blank.
That field is the whole answer:

| `license_model` | what it means | correct treatment |
|---|---|---|
| **License Included** | the Oracle licence cost **is inside the AWS hourly rate** | splittable and comparable, exactly like Windows |
| **Bring your own license** | the customer pays Oracle directly; the AWS rate contains **no** licence | **$0 on both sides** — they keep the same licence on GCP |

Neither case is "unmodellable". One is a split; the other is genuinely zero.

**The bug:** in `apply_static_mappings.py` (~line 8205) the Oracle branch
matches on the product/engine string and returns immediately, **before** the
`if "byol" in license_model` check ten lines below it. So *every* Oracle row —
including License Included — is labelled:

```
"Oracle license — BYOL; not modeled, add separately"
```

For a License Included row that statement is simply wrong, and it silently
drops real licence cost out of the comparison.

### The governing rule for ALL licence rows

Model a licence only when **both** clouds sell it natively. Verified against
`data/catalog.duckdb`:

| licence | GCP SKUs | treatment |
|---|---:|---|
| Windows Server | 111 | both sell it -> price both sides *(already built)* |
| SQL Server | 1,725 | both sell it -> price both sides *(already built)* |
| Ubuntu Pro | 195 | both sell it -> price both sides *(already built)* |
| RHEL | 52 | both sell it -> price both sides *(already built)* |
| SUSE / SLES | 44 | both sell it -> price both sides *(already built)* |
| **Oracle DB** | **0** | **AWS sells it, GCP does not** |

The 6 catalog SKUs matching "Oracle" are all **Backup and DR** products —
`BackupDR: Management - In GCP - Oracle`, `Backup and DR (for Oracle) CUD` and
similar. **None is an Oracle database licence.** GCP does not sell one.

So for any licence GCP does not offer, **do not project a GCP cost**. A number
there would be fabricated, and it misleads in the most damaging direction: the
customer plans around a GCP licence spend that does not exist, when in reality
they must bring their own licence, use a third party, or move to Bare Metal
Solution / Oracle Database@Google Cloud.

Show it as a **decision row**, not a projected cost: the AWS licence amount,
"no native GCP equivalent", and what the options are. That is more useful to
the customer than any number we could invent.

### The fix

1. **Read `license_model` first.** Restructure the Oracle branch to branch on
   it:
   - `License Included` → split the AWS cost using the same mechanism as
     Windows (`license_split.py`). On the GCP side there is no managed Oracle
     licence SKU, so emit a **passthrough** licence component carrying the
     split-out licence amount with reason `no_gcp_equivalent` and a note
     saying Oracle licensing must be carried over or renegotiated.
   - `Bring your own license` (or blank with a BYOL marker) → `strategy =
     "ignore"`, **$0**, note: *"Oracle licensed BYOL; the same licence applies
     on GCP, so no licence cost is projected on either side."*
2. **Do not invent a GCP Oracle licence rate.** There isn't a public one that
   applies generally; Oracle on GCP is Bare Metal Solution or
   Oracle Database@Google Cloud, both contract-specific.
3. **Surface it.** The customer-facing coverage statement names Oracle when
   present and states the amount excluded or carried, so the reader knows the
   size of what is not priced rather than only that something is.

### Tests
1. an Oracle row with `license_model = "License Included"` produces a licence
   component carrying the split amount — **not** the BYOL note
2. an Oracle row with BYOL produces `strategy = "ignore"` at $0 with the
   BYOL note
3. neither path fabricates a GCP Oracle licence rate
4. the coverage statement names Oracle and the amount when such rows exist

---

## ITEM 5 — Constrained-ratio estimation instead of passthrough

### The idea

Some AWS charges do not state a dimension that GCP needs in order to price
anything — an Aurora ACU does not say how many vCPU and how much RAM sit behind
it; a NAT gateway-hour does not say how many VMs are behind it. Today most of
those rows end up as **passthrough**, carrying the AWS cost unchanged.

**Passthrough is not neutral. It asserts GCP costs exactly what AWS costs —
a ratio of precisely 1.0.** That is itself an assumption, and a worse one than
a constrained estimate, because it is invisible and it can never be wrong in a
way anyone notices.

So: estimate instead, but only along ratios GCP actually sells.

### The three rules

**1. The ratio comes from a closed, allowed set — never a free number.**

GCP predefined machine families have fixed vCPU:RAM shapes. These are already
encoded in `scripts/candidate_pool.py` as `ram_per_vcpu`:

```
C4, C4D, C4A, T2A, N2D   ->  4
N4, N2, E2               ->  8
M3                       -> 16
```

So the allowed set for a general compute target is **{4, 8, 16}**. An admin
must not be able to enter `0.8` or `7` — those are not machines you can buy.
**Derive the set from that table at load time** rather than hardcoding it, so a
new GCP family updates the options automatically.

Where a target legitimately allows a continuous range (custom machine types),
declare an explicit `[min, max]` instead of a set, and clamp to it.

**2. There is still an absolute floor and ceiling.**

Even inside an allowed set, declare `min_ratio` and `max_ratio` per assumption
group as a backstop against a future family with an absurd shape, and against
a value written directly into the database. Out-of-range on read -> use the
default and emit `diag(WARN, "assumption_ratio_out_of_band", ...)` (Item 4.4).

**3. The default is the allowed ratio whose price lands nearest the AWS cost.**

For each candidate ratio in the allowed set, price the row and compute
`gcp_cost / aws_cost`. **Pick the candidate whose ratio is closest to 1.0.**

This is defensible precisely *because* the choice set is tiny and physical:
you are not tuning a free parameter until the answer looks nice, you are
choosing which of three real GCP machine shapes best matches what the customer
was actually paying for. With only three options the bias is bounded, and it
replaces a passthrough that silently asserted 1.0 anyway.

**Record the chosen ratio and why.** Every such row carries a note:

> "Aurora ACU sizing is not stated on the AWS bill. Priced as 1 ACU = 2 vCPU +
> 16 GiB (1:8, memory-optimised — the closest available GCP shape). This is a
> disclosed assumption and is adjustable in admin settings."

### What this does NOT change

- **Where AWS documents the dimension, use the document, not the calibration.**
  Aurora's ACU shape is documented; keep the documented value as the default
  and let calibration only confirm it is sane. Calibration is for dimensions
  nobody publishes.
- Do **not** calibrate anything that is already priced against a real SKU with
  a known unit. This applies only to rows that would otherwise be passthrough
  for want of a dimension.

### Admin surface

- The assumption editor offers a **dropdown of allowed ratios**, not a free
  numeric field, for any assumption in a ratio group.
- Each option shows what it means: `1:4 (compute-optimised, C4/N2D)`,
  `1:8 (balanced, N2/N4/E2)`, `1:16 (memory-optimised, M3)`.
- "Reset to defaults" restores the calibrated default from §3.

### Tests
1. the allowed set is derived from `candidate_pool.py`, not hardcoded — adding
   a family with a new `ram_per_vcpu` adds an option
2. an admin cannot set a ratio outside the allowed set (API rejects it)
3. the default selection picks the allowed ratio whose price ratio is nearest
   1.0 on a fixture with known prices
4. a row priced this way carries a note naming the ratio and that it is an
   assumption
5. a row that was passthrough **for want of a dimension** is now estimated;
   a row that is passthrough for `no_gcp_equivalent` is **untouched**

---

## 2. Verification

```bash
cd /c/Users/arnav/Downloads/cur-web-AGY && go build ./... && go test ./internal/...
cd skill/aws-gcp-cost-projection && python -m pytest tests/ -q
```

- Record baselines first; Python must end at **1,130 + your new tests**.
- **Projection totals must not change** on bills with no metal, no Oracle and
  default assumptions. Render before and after and compare.
- On a bill **with** metal rows, totals **will** change — that is the point.
  Report the before/after and explain which rows moved and why.

---

## 3. Do NOT

- Do **not** invent a `license_fraction` for metal or accelerator types
  (Rule 1).
- Do **not** remove the existing per-value min/max — Item 4 adds a layer.
- Do **not** let a bad stored ratio reach a projection silently (Item 4.4).
- Do **not** price Oracle licensing.
- Do **not** leave `.metal` flowing through the ordinary VM path once you have
  picked a strategy.
- Do **not** rebuild `license_split.py` or the existing GCP licence components
  — they work.

---

## 4. Done

- [ ] Item 4: ratio groups declared, validated on write **and** on read, reset
      verified authoritative, 5 tests
- [ ] Item 1: metal sized from specs; explicit mapping strategy; no fabricated
      licence; 3 tests
- [ ] Item 2: accelerator types excluded from licence split, assumption
      disclosed
- [ ] Item 3: Oracle branches on `license_model` — License Included split,
      BYOL zeroed; named and quantified in coverage; 4 tests
- [ ] Item 5: allowed-ratio sets derived from GCP family data; admin limited
      to a dropdown; defaults calibrated to nearest-1.0; every estimated row
      discloses its assumption; 5 tests
- [ ] Suites green; non-metal/non-Oracle totals unchanged; metal and
      newly-estimated rows explained in the summary
