// internal/jobs/backfill.go
//
// One-shot backfill that walks every job's projection.duckdb on backend
// startup and ensures a run_results table exists with at least one row
// (the "initial" projection synthesized from current data). Lets the new
// /runs endpoint return non-empty results for jobs that completed before
// the skill started populating run_results itself.
//
// Idempotent: a db that already has run_results with >=1 row is skipped.
// Errors per job are logged and swallowed so a single bad job dir can't
// crash startup.
package jobs

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// BackfillRunResults walks <jobsDir>/*/projection-audit/projection.duckdb
// and inserts a synthetic "initial" run row for any db lacking run_results.
// Called once at backend startup, after db.Open but before the orphan
// reaper. Always returns nil (per-job errors are logged) so a corrupt job
// dir can't crash the process.
func BackfillRunResults(jobsDir string) error {
	entries, err := os.ReadDir(jobsDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		jobID := e.Name()
		dbPath := filepath.Join(jobsDir, jobID, "projection-audit", "projection.duckdb")
		if _, err := os.Stat(dbPath); err != nil {
			// No projection.duckdb — either a brand new job that hasn't
			// gotten past Phase 1, or a legacy failed/wiped job. Nothing
			// to backfill.
			continue
		}
		if err := backfillOneJob(jobsDir, jobID, dbPath); err != nil {
			log.Printf("backfill %s: %v", jobID, err)
		}
	}
	return nil
}

func backfillOneJob(jobsDir, jobID, dbPath string) error {
	// Skip if run_results already populated.
	exists, err := tableExists(dbPath, "run_results")
	if err != nil {
		return fmt.Errorf("tableExists: %w", err)
	}
	if exists {
		count, err := runResultsRowCount(dbPath)
		if err != nil {
			return fmt.Errorf("row count: %w", err)
		}
		if count >= 1 {
			log.Printf("backfill %s: skipped (run_results has %d rows)", jobID, count)
			return nil
		}
	}

	// Derive the report.html mtime — used both for the synthesized run_id
	// and as ts_utc. We prefer the legacy unsuffixed path inside
	// projection-audit/ since that's what every pre-this-release job has.
	reportPath := filepath.Join(jobsDir, jobID, "projection-audit", "report.html")
	info, err := os.Stat(reportPath)
	if err != nil {
		return fmt.Errorf("stat report.html: %w", err)
	}
	ts := info.ModTime().UTC()
	runID := "backfill-" + ts.Format("20060102T150405Z")
	tsLiteral := ts.Format("2006-01-02 15:04:05")

	// We open the db read-write here. duckdb CLI invocation; CREATE TABLE
	// IF NOT EXISTS handles the case where the table was created (but
	// empty) by a partial skill run.
	sql := fmt.Sprintf(`
CREATE TABLE IF NOT EXISTS run_results (
  run_id        TEXT PRIMARY KEY,
  ts_utc        TIMESTAMP,
  run_type      TEXT,
  instruction   TEXT,
  aws_total     DOUBLE,
  gcp_od        DOUBLE,
  gcp_1yr_cud   DOUBLE,
  gcp_3yr_cud   DOUBLE,
  report_html   TEXT,
  report_md     TEXT,
  summary_md    TEXT,
  mapped_rows   INTEGER,
  passthroughs  INTEGER,
  confidence    TEXT
);
INSERT INTO run_results
SELECT
  '%s' AS run_id,
  TIMESTAMP '%s' AS ts_utc,
  'initial' AS run_type,
  NULL AS instruction,
  COALESCE((SELECT SUM(aws_amortized_cost) FROM aws_li_catalog), 0) AS aws_total,
  COALESCE((SELECT SUM(gcp_projected_cost) FROM gcp_projection WHERE is_workload), 0) AS gcp_od,
  COALESCE((SELECT SUM(gcp_cost_1yr_cud) FROM gcp_projection WHERE is_workload), 0) AS gcp_1yr_cud,
  COALESCE((SELECT SUM(gcp_cost_3yr_cud) FROM gcp_projection WHERE is_workload), 0) AS gcp_3yr_cud,
  'projection-audit/report.html' AS report_html,
  'projection-audit/report.md' AS report_md,
  NULL AS summary_md,
  COALESCE((SELECT COUNT(*) FROM aws_li_to_gcp_li), 0) AS mapped_rows,
  COALESCE((SELECT COUNT(*) FROM aws_li_to_gcp_li WHERE strategy='passthrough'), 0) AS passthroughs,
  NULL AS confidence
WHERE NOT EXISTS (SELECT 1 FROM run_results WHERE run_id='%s');
`, runID, tsLiteral, runID)

	cmd := exec.Command("duckdb", dbPath, "-c", sql)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("duckdb insert: %w: %s", err, strings.TrimSpace(string(out)))
	}
	log.Printf("backfill %s: inserted synthetic initial row %s", jobID, runID)
	return nil
}

func runResultsRowCount(dbPath string) (int, error) {
	out, err := runDuckDB(dbPath, "SELECT COUNT(*) AS n FROM run_results")
	if err != nil {
		return 0, err
	}
	if len(out) == 0 || strings.TrimSpace(string(out)) == "" {
		return 0, nil
	}
	var rows []map[string]any
	if err := json.Unmarshal(out, &rows); err != nil {
		return 0, err
	}
	if len(rows) == 0 {
		return 0, nil
	}
	if i := nullableInt(rows[0]["n"]); i != nil {
		return *i, nil
	}
	return 0, nil
}

