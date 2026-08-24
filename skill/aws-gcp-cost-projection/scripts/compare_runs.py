#!/usr/bin/env python3
import sys
import duckdb

def compare_runs(baseline_db, new_db):
    print(f"Comparing Baseline: {baseline_db}")
    print(f"       to New Run: {new_db}")
    print("="*60)
    
    con = duckdb.connect(':memory:')
    con.execute(f"ATTACH '{baseline_db}' AS baseline (READ_ONLY)")
    con.execute(f"ATTACH '{new_db}' AS newrun (READ_ONLY)")
    
    try:
        # Check Total AWS Cost (should be identical)
        res = con.execute("SELECT sum(aws_amortized_cost) FROM baseline.aws_li_catalog WHERE is_workload").fetchone()
        baseline_aws = res[0] if res and res[0] else 0.0
        
        # Check Total GCP Projected Cost
        res = con.execute("SELECT sum(gcp_projected_cost) FROM baseline.gcp_projection WHERE is_workload").fetchone()
        baseline_gcp = res[0] if res and res[0] else 0.0
        
        res = con.execute("SELECT sum(gcp_projected_cost) FROM newrun.gcp_projection WHERE is_workload").fetchone()
        newrun_gcp = res[0] if res and res[0] else 0.0
        
        print(f"Total GCP Projected Cost:")
        print(f"  Baseline: ${baseline_gcp:,.2f}")
        print(f"  New Run:  ${newrun_gcp:,.2f}")
        print(f"  Diff:     ${newrun_gcp - baseline_gcp:,.2f}")
        print("-" * 60)
        
        # SKU Flips
        # Join aws_li_to_gcp_li on aws_li_key
        flips_query = """
        SELECT count(*) FROM baseline.aws_li_to_gcp_li b
        JOIN newrun.aws_li_to_gcp_li n ON b.aws_li_key = n.aws_li_key
        WHERE b.gcp_sku_id != n.gcp_sku_id
           OR b.strategy != n.strategy
        """
        flips = con.execute(flips_query).fetchone()[0]
        
        total_mappings = con.execute("SELECT count(*) FROM newrun.aws_li_to_gcp_li").fetchone()[0]
        
        print(f"Mapping Differences (SKU or Strategy changed): {flips} / {total_mappings} rows")
        print("-" * 60)
        
        # List top 5 flipped mappings
        if flips > 0:
            print("Top 5 Flipped Rows (by AWS Cost):")
            top_flips = """
            SELECT c.aws_resource_type, c.aws_amortized_cost, b.gcp_sku_id as base_sku, n.gcp_sku_id as new_sku
            FROM baseline.aws_li_to_gcp_li b
            JOIN newrun.aws_li_to_gcp_li n ON b.aws_li_key = n.aws_li_key
            JOIN baseline.aws_li_catalog c ON b.aws_li_key = c.aws_li_key
            WHERE b.gcp_sku_id != n.gcp_sku_id
            ORDER BY c.aws_amortized_cost DESC
            LIMIT 5
            """
            for row in con.execute(top_flips).fetchall():
                print(f"  ${row[1]:.2f} {row[0]}: {row[2]} -> {row[3]}")
            print("-" * 60)
            
        print("Done.")
    except duckdb.Error as e:
        print(f"Error querying databases: {e}")
        print("Note: If 'gcp_projection' doesn't exist, it means Phase 5 was skipped in one of the runs.")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 compare_runs.py <baseline.duckdb> <new_run.duckdb>")
        sys.exit(1)
    compare_runs(sys.argv[1], sys.argv[2])
