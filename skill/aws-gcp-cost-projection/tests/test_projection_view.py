import duckdb
import pytest
from projection_view import create_projection_view, compute_dominant_region


@pytest.fixture
def mock_db():
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY,
            product TEXT,
            aws_region TEXT,
            gcp_region TEXT,
            line_item_type TEXT,
            pricing_model TEXT,
            is_workload BOOLEAN,
            total_usage DOUBLE,
            aws_amortized_cost DOUBLE
        );
        CREATE TABLE aws_li_to_gcp_li (
            aws_li_key TEXT,
            strategy TEXT,
            gcp_service TEXT,
            gcp_sku_id TEXT,
            component TEXT,
            unit_multiplier DOUBLE,
            projection_note TEXT
        );
    """)
    import row_state
    row_state.ensure_schema(conn)
    conn.execute("""
        CREATE TABLE gcp_sku_rates (
            gcp_sku_id TEXT,
            gcp_service TEXT,
            gcp_sku_name TEXT,
            resource_family TEXT,
            resource_group TEXT,
            pricing_type TEXT,
            region TEXT,
            unit TEXT,
            rate_usd DOUBLE,
            source TEXT,
            audit_url TEXT
        );
    """)
    return conn


def test_dominant_region_computation(mock_db):
    conn = mock_db
    conn.execute("""
        INSERT INTO aws_li_catalog VALUES
            ('k1', 'Amazon EC2', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 100, 10),
            ('k2', 'Amazon EC2', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 100, 10),
            ('k3', 'Amazon EC2', 'us-west-2', 'us-west1', 'Usage', 'OnDemand', true, 50, 5),
            ('k4', 'Amazon CloudFront', 'global', 'global', 'Usage', 'OnDemand', true, 10, 1);
    """)
    dom = compute_dominant_region(conn)
    assert dom == "us-east1"


def test_global_row_fallback_to_dominant_region(mock_db):
    conn = mock_db
    # Bill dominated by us-east1
    conn.execute("""
        INSERT INTO aws_li_catalog VALUES
            ('k1', 'Amazon EC2', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 100, 10),
            ('k2', 'Amazon EC2', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 100, 10),
            ('k_global_no_glob_sku', 'Amazon Route 53', 'global', 'global', 'Usage', 'OnDemand', true, 100, 5),
            ('k_global_has_glob_sku', 'AWS Shield', 'global', 'global', 'Usage', 'OnDemand', true, 1, 100);
    """)
    conn.execute("""
        INSERT INTO aws_li_to_gcp_li (aws_li_key, strategy, gcp_service, gcp_sku_id, component, unit_multiplier, projection_note) VALUES
            ('k1', 'map', 'Compute Engine', 'SKU-VM', 'core', 1, NULL),
            ('k2', 'map', 'Compute Engine', 'SKU-VM', 'core', 1, NULL),
            ('k_global_no_glob_sku', 'map', 'Cloud DNS', 'SKU-REG-DNS', 'query', 1, NULL),
            ('k_global_has_glob_sku', 'map', 'Cloud Armor', 'SKU-GLOB-ARMOR', 'rule', 1, NULL);
    """)
    # SKU-REG-DNS only has rate in us-east1 (no global rate)
    # SKU-GLOB-ARMOR has rate in global ($100) and us-east1 ($120)
    conn.execute("""
        INSERT INTO gcp_sku_rates VALUES
            ('SKU-VM', 'Compute Engine', 'VM', 'Compute', 'CPU', 'OnDemand', 'us-east1', 'h', 0.10, 'cat', 'url'),
            ('SKU-REG-DNS', 'Cloud DNS', 'DNS', 'Network', 'DNS', 'OnDemand', 'us-east1', 'query', 0.05, 'cat', 'url'),
            ('SKU-GLOB-ARMOR', 'Cloud Armor', 'Armor', 'Security', 'Armor', 'OnDemand', 'global', 'mo', 100.0, 'cat', 'url'),
            ('SKU-GLOB-ARMOR', 'Cloud Armor', 'Armor', 'Security', 'Armor', 'OnDemand', 'us-east1', 'mo', 120.0, 'cat', 'url');
    """)

    conn.execute("""
        UPDATE aws_li_to_gcp_li
        SET rate_region_basis = 'global', rate_region_used = 'us-east1'
        WHERE aws_li_key = 'k_global_no_glob_sku'
    """)
    import render_notes
    render_notes.render_notes_for_db(conn)

    create_projection_view(conn)

    # 1. k_global_no_glob_sku should price at dominant region rate ($0.05 * 100 = 5.0)
    row_no_glob = conn.execute("""
        SELECT gcp_projected_cost, projection_note
        FROM gcp_projection
        WHERE aws_li_key = 'k_global_no_glob_sku'
    """).fetchone()
    assert row_no_glob[0] == pytest.approx(5.0)
    assert "priced at the us-east1 rate" in row_no_glob[1]

    # 2. k_global_has_glob_sku should price at global rate ($100.0 * 1 = 100.0), NOT us-east1 rate
    row_glob = conn.execute("""
        SELECT gcp_projected_cost, projection_note
        FROM gcp_projection
        WHERE aws_li_key = 'k_global_has_glob_sku'
    """).fetchone()
    assert row_glob[0] == pytest.approx(100.0)
    assert row_glob[1] is None or "global charge; priced at" not in row_glob[1]


def test_non_committable_items_cud_fallback_to_ondemand(mock_db):
    conn = mock_db
    conn.execute("""
        INSERT INTO aws_li_catalog VALUES
            ('k_storage', 'Amazon S3', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 1000, 20);
        INSERT INTO aws_li_to_gcp_li (aws_li_key, strategy, gcp_service, gcp_sku_id, component, unit_multiplier, projection_note) VALUES
            ('k_storage', 'map', 'Cloud Storage', 'SKU-GCS', 'storage', 1, NULL);
        INSERT INTO gcp_sku_rates VALUES
            ('SKU-GCS', 'Cloud Storage', 'Standard', 'Storage', 'GCS', 'OnDemand', 'us-east1', 'GB-mo', 0.020, 'cat', 'url');
    """)
    create_projection_view(conn)
    row = conn.execute("""
        SELECT gcp_projected_cost, gcp_cost_1yr_cud, gcp_cost_3yr_cud
        FROM gcp_projection
        WHERE aws_li_key = 'k_storage'
    """).fetchone()
    assert row[0] == pytest.approx(20.0)
    # P-12: Non-committable rows fall back to on-demand rate in CUD projections
    assert row[1] == pytest.approx(20.0)
    assert row[2] == pytest.approx(20.0)


def test_od_vs_od_symmetry_and_basis(mock_db):
    """WORKORDER_od_vs_od_sp_ri: Assert OD-vs-OD symmetry and od_basis tracking."""
    conn = mock_db
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS public_on_demand_cost DOUBLE;")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS aws_od_cost DOUBLE;")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS sp_ri_discount_pct DOUBLE;")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS od_basis VARCHAR;")

    conn.execute("""
        INSERT INTO aws_li_catalog (
            aws_li_key, product, aws_region, gcp_region, line_item_type,
            pricing_model, is_workload, total_usage, aws_amortized_cost,
            public_on_demand_cost, aws_od_cost, sp_ri_discount_pct, od_basis
        ) VALUES
            ('k_cur_od', 'Amazon EC2', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 100, 50.0, 50.0, 50.0, 0.0, 'public_od'),
            ('k_cur_sp', 'Amazon EC2', 'us-east-1', 'us-east1', 'SavingsPlanCoveredUsage', 'SavingsPlan', true, 100, 30.0, 50.0, 50.0, 0.40, 'public_od'),
            ('k_recon_sp', 'Amazon EC2', 'us-east-1', 'us-east1', 'SavingsPlanCoveredUsage', 'SavingsPlan', true, 100, 31.0, NULL, 50.0, 0.38, 'reconstructed_default'),
            ('k_pt_od', 'Amazon Custom', 'us-east-1', 'us-east1', 'Usage', 'OnDemand', true, 1, 100.0, 100.0, 100.0, 0.0, 'public_od');
    """)

    conn.execute("""
        INSERT INTO aws_li_to_gcp_li (aws_li_key, strategy, gcp_service, gcp_sku_id, component, unit_multiplier, projection_note) VALUES
            ('k_cur_od', 'map', 'Compute Engine', 'SKU-VM-OD', 'core', 1, NULL),
            ('k_cur_sp', 'map', 'Compute Engine', 'SKU-VM-OD', 'core', 1, NULL),
            ('k_recon_sp', 'map', 'Compute Engine', 'SKU-VM-OD', 'core', 1, NULL),
            ('k_pt_od', 'passthrough', 'Manual Review', NULL, NULL, 1.0, NULL);
    """)

    conn.execute("""
        INSERT INTO gcp_sku_rates VALUES
            ('SKU-VM-OD', 'Compute Engine', 'VM', 'Compute', 'CPU', 'OnDemand', 'us-east1', 'h', 0.50, 'cat', 'url'),
            ('SKU-VM-OD', 'Compute Engine', 'VM', 'Compute', 'CPU', 'Commit1Yr', 'us-east1', 'h', 0.35, 'cat', 'url'),
            ('SKU-VM-OD', 'Compute Engine', 'VM', 'Compute', 'CPU', 'Commit3Yr', 'us-east1', 'h', 0.25, 'cat', 'url');
    """)

    create_projection_view(conn)

    rows = conn.execute("""
        SELECT aws_li_key, aws_od_cost, aws_amortized_cost, gcp_projected_cost, gcp_cost_1yr_cud, gcp_cost_3yr_cud, od_basis
        FROM gcp_projection
        ORDER BY aws_li_key
    """).fetchall()

    res = {r[0]: r for r in rows}

    # k_cur_od: public_od basis, OD = 50.0, GCP OD = 50.0, CUD1 = 35.0, CUD3 = 25.0
    assert res['k_cur_od'][1] == pytest.approx(50.0)
    assert res['k_cur_od'][3] == pytest.approx(50.0)
    assert res['k_cur_od'][4] == pytest.approx(35.0)
    assert res['k_cur_od'][5] == pytest.approx(25.0)
    assert res['k_cur_od'][6] == 'public_od'

    # k_cur_sp: public_od basis (from public_on_demand_cost = 50.0 despite amortized = 30.0)
    assert res['k_cur_sp'][1] == pytest.approx(50.0)
    assert res['k_cur_sp'][2] == pytest.approx(30.0)
    assert res['k_cur_sp'][3] == pytest.approx(50.0)
    assert res['k_cur_sp'][6] == 'public_od'

    # k_recon_sp: reconstructed-from-default basis
    assert res['k_recon_sp'][1] == pytest.approx(50.0)
    assert res['k_recon_sp'][6] == 'reconstructed_default'

    # k_pt_od: passthrough symmetrically uses aws_od_cost
    assert res['k_pt_od'][1] == pytest.approx(100.0)
    assert res['k_pt_od'][3] == pytest.approx(100.0)
    assert res['k_pt_od'][4] == pytest.approx(100.0)
    assert res['k_pt_od'][5] == pytest.approx(100.0)



def _od_catalog(conn):
    conn.execute("""
        CREATE TABLE aws_li_catalog (
            aws_li_key TEXT PRIMARY KEY, product TEXT, usage_type TEXT, operation TEXT,
            pricing_model TEXT, line_item_type TEXT, aws_amortized_cost DOUBLE,
            public_on_demand_cost DOUBLE, aws_od_cost DOUBLE, sp_ri_discount_pct DOUBLE,
            od_basis VARCHAR
        );
        INSERT INTO aws_li_catalog
            (aws_li_key, product, usage_type, operation, pricing_model, line_item_type,
             aws_amortized_cost, public_on_demand_cost) VALUES
            ('od',   'Amazon EC2', 'BoxUsage:m5.large', '', 'OnDemand', 'Usage', 50.0, NULL),
            ('pub',  'Amazon EC2', 'BoxUsage:m5.large', '', 'SavingsPlan', 'SavingsPlanCoveredUsage', 30.0, 50.0),
            ('ri1',  'Amazon EC2', 'BoxUsage:m5.large', '', 'Reserved', 'DiscountedUsage', 62.0, NULL),
            ('sp',   'Amazon EC2', 'BoxUsage:m5.large', '', 'SavingsPlan', 'SavingsPlanCoveredUsage', 62.0, NULL),
            ('none', 'Amazon EC2', 'BoxUsage:m5.large', '', '', 'Usage', 10.0, NULL);
    """)


def test_apply_od_basis_honours_configured_discount():
    """F-A regression: ingest's OD reconstruction must read the SAME assumptions
    projection_view uses, not a hardcoded rate — otherwise an operator override is
    silently ignored and the stale value wins the view's COALESCE."""
    import ingest

    # Default rate (0.38) -> aws_od_cost = 62 / (1-0.38) = 100.0
    c1 = duckdb.connect(":memory:"); _od_catalog(c1)
    ingest.apply_od_basis(c1, {"aws_ri_1yr_discount_pct": 0.38})
    r1 = {k: (od, basis) for k, od, basis in
          c1.execute("SELECT aws_li_key, aws_od_cost, od_basis FROM aws_li_catalog").fetchall()}
    assert r1['od'][0] == pytest.approx(50.0) and r1['od'][1] == 'public_od'
    assert r1['pub'][0] == pytest.approx(50.0) and r1['pub'][1] == 'public_od'       # real public OD wins
    assert r1['ri1'][0] == pytest.approx(100.0) and r1['ri1'][1] == 'reconstructed_default'
    assert r1['none'][1] == 'amortized_fallback' and r1['none'][0] == pytest.approx(10.0)  # no rate invented

    # Operator override (0.50) MUST change the reconstructed number -> 62/0.5 = 124.0
    c2 = duckdb.connect(":memory:"); _od_catalog(c2)
    ingest.apply_od_basis(c2, {"aws_ri_1yr_discount_pct": 0.50})
    od_ri1 = c2.execute("SELECT aws_od_cost FROM aws_li_catalog WHERE aws_li_key='ri1'").fetchone()[0]
    assert od_ri1 == pytest.approx(124.0), "configured discount not honoured — hardcoded rate leaked"
