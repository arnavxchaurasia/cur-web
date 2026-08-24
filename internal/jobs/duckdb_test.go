// internal/jobs/duckdb_test.go
package jobs_test

import (
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/facets/cur-web/internal/jobs"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// requireDuckDB skips the test when the duckdb CLI isn't available (CI
// environments may not have it). Real tests rely on the binary; we don't
// pull in a Go driver intentionally.
func requireDuckDB(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("duckdb"); err != nil {
		t.Skip("duckdb CLI not on PATH; skipping")
	}
}

// createFixtureDB builds a minimal projection.duckdb shaped like the one
// the skill writes: run_results + aws_li_catalog. Returns the absolute
// path to the file.
func createFixtureDB(t *testing.T, includeRunResults bool, includeAWS bool) string {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "projection.duckdb")
	var sql string
	if includeRunResults {
		sql += `
CREATE TABLE run_results (
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
INSERT INTO run_results VALUES
  ('20260101T000000Z', '2026-01-01 00:00:00', 'initial', NULL,
   100.0, 80.0, 70.0, 60.0,
   'projection-audit/report-20260101T000000Z.html',
   'projection-audit/report-20260101T000000Z.md',
   NULL, 42, 3, 'medium'),
  ('20260102T000000Z', '2026-01-02 00:00:00', 'refinement', 'use SUDs',
   100.0, 75.0, 65.0, 55.0,
   'projection-audit/report-20260102T000000Z.html',
   'projection-audit/report-20260102T000000Z.md',
   'projection-audit/summary-20260102T000000Z.md', 42, 3, 'high');
`
	}
	if includeAWS {
		sql += `
CREATE TABLE aws_li_catalog (aws_amortized_cost DOUBLE, is_workload BOOLEAN);
INSERT INTO aws_li_catalog VALUES (10.5, true), (20.0, true), (1000.0, false);
`
	}
	if sql != "" {
		cmd := exec.Command("duckdb", dbPath, "-c", sql)
		out, err := cmd.CombinedOutput()
		require.NoErrorf(t, err, "duckdb create: %s", string(out))
	}
	return dbPath
}

func TestQueryRunResults_HappyPath(t *testing.T) {
	requireDuckDB(t)
	dbPath := createFixtureDB(t, true, false)

	runs, err := jobs.QueryRunResults(dbPath)
	require.NoError(t, err)
	require.Len(t, runs, 2)

	// Latest first (ORDER BY ts_utc DESC)
	assert.Equal(t, "20260102T000000Z", runs[0].RunID)
	assert.Equal(t, "refinement", runs[0].RunType)
	require.NotNil(t, runs[0].Instruction)
	assert.Equal(t, "use SUDs", *runs[0].Instruction)
	require.NotNil(t, runs[0].GCPOD)
	assert.InDelta(t, 75.0, *runs[0].GCPOD, 0.001)
	require.NotNil(t, runs[0].SummaryMD)
	assert.Equal(t, "projection-audit/summary-20260102T000000Z.md", *runs[0].SummaryMD)

	assert.Equal(t, "20260101T000000Z", runs[1].RunID)
	assert.Equal(t, "initial", runs[1].RunType)
	assert.Nil(t, runs[1].Instruction)
	assert.Nil(t, runs[1].SummaryMD)
}

func TestQueryRunResults_MissingDB(t *testing.T) {
	runs, err := jobs.QueryRunResults("/nonexistent/path/projection.duckdb")
	require.NoError(t, err)
	assert.Len(t, runs, 0)
}

func TestQueryRunResults_NoTable(t *testing.T) {
	requireDuckDB(t)
	// db that exists but has no run_results table — only aws_li_catalog
	dbPath := createFixtureDB(t, false, true)

	runs, err := jobs.QueryRunResults(dbPath)
	require.NoError(t, err)
	assert.Len(t, runs, 0)
}

func TestQueryAWSSpend(t *testing.T) {
	requireDuckDB(t)
	dbPath := createFixtureDB(t, false, true)

	spend, err := jobs.QueryAWSSpend(dbPath)
	require.NoError(t, err)
	// 10.5 + 20.0 + 1000.0 = ALL rows. Headline matches the bill
	// grand-total post-discount; the negative discount/credit rows
	// (is_workload=FALSE) net themselves in/out. See QueryAWSSpend
	// doc comment + parv@facets Rooter PDF Test 2 case.
	assert.InDelta(t, 1030.5, spend, 0.001)
}

func TestQueryAWSSpend_MissingDB(t *testing.T) {
	spend, err := jobs.QueryAWSSpend("/nonexistent/path/projection.duckdb")
	require.NoError(t, err)
	assert.Equal(t, 0.0, spend)
}

func TestQueryAWSSpend_NoTable(t *testing.T) {
	requireDuckDB(t)
	// db with only run_results (no aws_li_catalog)
	dbPath := createFixtureDB(t, true, false)

	spend, err := jobs.QueryAWSSpend(dbPath)
	require.NoError(t, err)
	assert.Equal(t, 0.0, spend)
}
