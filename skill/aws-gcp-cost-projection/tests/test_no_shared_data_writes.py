#!/usr/bin/env python3
"""
tests/test_no_shared_data_writes.py — Enforce that per-job scripts never write
to the shared skill/data/ directory.

Background:
Jobs running concurrently (or across ephemeral container deploys) must never
mutate shared state in the skill/data directory. Shared writes cause data loss,
empty-read race conditions, and do not persist on ephemeral container disks.

Permitted writers are strictly pinned to maintenance tools that run offline
or during image build, never per job.
"""
import ast
import glob
import os
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
DATA_DIR = os.path.join(REPO_ROOT, "data")

# Pinned allow-list of offline maintenance tools that legitimately write to data/.
# Per-job scripts MUST NOT be added here.
ALLOWED_MAINTENANCE_WRITERS = {
    # Specifically pinned by WORKORDER_learned_cache_promotion.md and WORKORDER_equivalence_crosswalk_review.md:
    "promote_learned_codes.py",
    "promote_equivalence.py",
    "refresh_aws_product_codes.py",
    "refresh_ec2_specs.py",
    "build_catalog_index.py",
    "prefetch_skus.py",
    "catalog_manager.py",
    # Developer tool, never invoked by the pipeline: regenerates
    # data/ratio_registry.json from candidate_pool.py so that Go (admin writes)
    # and Python (config_loader reads) validate against one source of truth.
    # test_assumptions.py asserts the generated file stays in sync.
    "gen_ratio_registry.py",
    # Additional offline maintenance tools (audit finding T4):
    "verify_compute_availability.py",  # offline fetch of gcp_compute_availability.json
    "weekly_catalog_refresh.py",       # Windows Task Scheduler wrapper logging to catalog_refresh.log
    # Writes data/CATALOG_META.json.lock around the refresh it triggers. Runs at
    # Docker build time (Dockerfile) and as a maintenance tool -- it is NOT a
    # per-job script, so the lock has a single writer.
    #
    # CAVEAT: its own module docstring claims it is "called by orchestrate.go as
    # a pre_llm_script before ingest". That is false today (grep orchestrate.go
    # for check_catalog_age -- no caller). If anyone ever makes the docstring
    # true, this lock becomes shared mutable state written by concurrent jobs
    # and this allow-list entry must be removed, not widened.
    "check_catalog_age.py",
}


def _is_job_local(expr: str) -> bool:
    return any(j in expr for j in ("JOB_DIR", "job_dir", "jobdir", "projection-audit"))


def _names_used(node: ast.AST) -> set[str]:
    """Identifiers referenced in an expression.

    Compared as identifiers, never as substrings: `r in val_str` lets a short
    root name match inside an unrelated identifier, and the fixed-point pass
    then cascades that one false hit across the whole file.
    """
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _collect(assigns, seed_roots: set[str], seed_files: set[str]):
    """Names bound to the shared data dir, or to files derived from it."""
    roots = set(seed_roots)
    files = set(seed_files)

    for stmt in assigns:
        val_str = ast.unparse(stmt.value)
        # e.g. DATA_DIR = os.path.join(SKILL_DIR, "data")
        if any(term in val_str for term in ('"data"', "'data'")) and any(
            p in val_str
            for p in ("SKILL_DIR", "_HERE", "__file__", "SCRIPTS_DIR", "REPO_ROOT", "os.path.dirname")
        ):
            if not _is_job_local(val_str):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        roots.add(t.id)

    changed = True
    while changed:
        changed = False
        for stmt in assigns:
            if _is_job_local(ast.unparse(stmt.value)):
                continue
            if not (_names_used(stmt.value) & (roots | files)):
                continue
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id not in files and t.id not in roots:
                    files.add(t.id)
                    changed = True

    return roots, files


def _find_shared_data_writes(filepath: str) -> list[tuple[int, str, str]]:
    """Scan a Python file for writes into shared data/ paths.

    Resolution is SCOPE-AWARE. A previous version propagated derived names
    across the whole file, so once any `path = <a data file>` existed in an
    8,700-line module, every `open(path, "w")` anywhere in it was reported --
    including job-local writes. Module-level names are visible everywhere;
    function-local names apply only inside their own function.

    Returns (line_number, called_function, target_expression).
    """
    with open(filepath, encoding="utf-8-sig") as f:
        src = f.read()
    tree = ast.parse(src, filename=filepath)

    # Module scope.
    mod_assigns = [n for n in tree.body if isinstance(n, ast.Assign)]
    mod_roots, mod_files = _collect(mod_assigns, set(), set())

    # Per-function scope, seeded from module scope.
    func_nodes = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    scope_of: dict[int, tuple[set[str], set[str]]] = {}
    owner: dict[int, int] = {}
    for fn in func_nodes:
        fn_assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)]
        scope_of[id(fn)] = _collect(fn_assigns, mod_roots, mod_files)
        for child in ast.walk(fn):
            owner.setdefault(id(child), id(fn))

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn_name = ""
        if isinstance(node.func, ast.Name):
            fn_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fn_name = node.func.attr
        if fn_name != "open" or not node.args:
            continue

        mode = "r"
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if not any(m in mode for m in ("w", "a", "+")):
            continue

        roots, files = scope_of.get(owner.get(id(node), 0), (mod_roots, mod_files))

        target_expr = ast.unparse(node.args[0])
        target_names = {n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)}
        if _is_job_local(target_expr):
            continue

        if target_names & (roots | files):
            violations.append((node.lineno, fn_name, target_expr))
        elif any(s in target_expr.lower() for s in ('"data/', "'data/", '"data\\', "'data\\\\")):
            violations.append((node.lineno, fn_name, target_expr))

    return violations


def test_no_shared_data_writes():
    """Verify that no script outside the allow-list opens shared data/ files for writing."""
    all_violations = {}
    py_files = glob.glob(os.path.join(SCRIPTS_DIR, "*.py"))

    for fpath in py_files:
        fname = os.path.basename(fpath)
        if fname in ALLOWED_MAINTENANCE_WRITERS:
            continue

        writes = _find_shared_data_writes(fpath)
        if writes:
            all_violations[fname] = writes

    msg_lines = []
    for fname, writes in sorted(all_violations.items()):
        for lineno, fn, target in writes:
            msg_lines.append(f"  {fname}:{lineno} -> {fn}({target})")

    assert not all_violations, (
        f"Found {len(all_violations)} script(s) performing writes into the shared data/ directory:\n"
        + "\n".join(msg_lines)
        + "\nJobs must write proposals to <job_dir>/projection-audit/ instead of mutating shared state."
    )


def test_resolve_unknown_product_codes_never_writes_shared_cache():
    """Explicitly verify resolve_unknown_product_codes.py has zero writes to data/."""
    target_script = os.path.join(SCRIPTS_DIR, "resolve_unknown_product_codes.py")
    writes = _find_shared_data_writes(target_script)
    assert not writes, f"resolve_unknown_product_codes.py must not write to shared data/: {writes}"
