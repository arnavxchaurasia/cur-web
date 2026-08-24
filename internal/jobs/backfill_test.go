// internal/jobs/backfill_test.go
package jobs_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/facets/cur-web/internal/jobs"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// makeLegacyJobDir builds a jobsDir/<id>/projection-audit/ layout with a
// projection.duckdb shaped like a pre-this-release skill run: has the
// data tables but NO run_results table, and a report.html on disk.
func makeLegacyJobDir(t *testing.T, jobsDir, jobID string) string {
	t.Helper()
	auditDir := filepath.Join(jobsDir, jobID, "projection-audit")
	require.NoError(t, os.MkdirAll(auditDir, 0755))

	// Write a minimal report.html so the backfill can stat it for mtime.
	reportPath := filepath.Join(auditDir, "report.html")
	require.NoError(t, os.WriteFile(reportPath, []byte("<html>legacy</html>"), 0644))
	// Pin mtime to a known UTC instant so the synthesized run_id is
	// deterministic.
	pinned := time.Date(2026, 5, 11, 12, 42, 0, 0, time.UTC)
	require.NoError(t, os.Chtimes(reportPath, pinned, pinned))

	dbPath := filepath.Join(auditDir, "projection.duckdb")
	sql := `
CREATE TABLE aws_li_catalog (aws_amortized_cost DOUBLE, is_workload BOOLEAN);
INSERT INTO aws_li_catalog VALUES (50.0, true), (1000.0, false);
CREATE TABLE gcp_projection (
  gcp_projected_cost DOUBLE,
  gcp_cost_1yr_cud DOUBLE,
  gcp_cost_3yr_cud DOUBLE,
  is_workload BOOLEAN
);
INSERT INTO gcp_projection VALUES (40.0, 35.0, 30.0, true);
CREATE TABLE aws_li_to_gcp_li (strategy TEXT);
INSERT INTO aws_li_to_gcp_li VALUES ('map'), ('map'), ('passthrough');
`
	cmd := exec.Command("duckdb", dbPath, "-c", sql)
	out, err := cmd.CombinedOutput()
	require.NoErrorf(t, err, "duckdb create: %s", string(out))
	return dbPath
}

func TestBackfillRunResults(t *testing.T) {
	requireDuckDB(t)
	jobsDir := t.TempDir()
	dbPath := makeLegacyJobDir(t, jobsDir, "job-legacy-1")

	// First run: should insert one synthesized row.
	require.NoError(t, jobs.BackfillRunResults(jobsDir))

	runs, err := jobs.QueryRunResults(dbPath)
	require.NoError(t, err)
	require.Len(t, runs, 1)

	r := runs[0]
	assert.Equal(t, "backfill-20260511T124200Z", r.RunID)
	assert.Equal(t, "initial", r.RunType)
	assert.Nil(t, r.Instruction)
	require.NotNil(t, r.AWSTotal)
	// 50.0 + 1000.0 = ALL rows. Matches the bill grand-total
	// (post-discount net), not just workload rows. See
	// QueryAWSSpend doc + parv@facets Rooter PDF Test 2.
	assert.InDelta(t, 1050.0, *r.AWSTotal, 0.001)
	require.NotNil(t, r.GCPOD)
	assert.InDelta(t, 40.0, *r.GCPOD, 0.001)
	require.NotNil(t, r.ReportHTML)
	assert.Equal(t, "projection-audit/report.html", *r.ReportHTML)
	assert.Nil(t, r.SummaryMD)
	require.NotNil(t, r.MappedRows)
	assert.Equal(t, 3, *r.MappedRows)
	require.NotNil(t, r.Passthroughs)
	assert.Equal(t, 1, *r.Passthroughs)

	// Second run: idempotent — must not insert a second row.
	require.NoError(t, jobs.BackfillRunResults(jobsDir))
	runs2, err := jobs.QueryRunResults(dbPath)
	require.NoError(t, err)
	assert.Len(t, runs2, 1, "backfill should be idempotent")
}

func TestBackfillRunResults_NoJobsDir(t *testing.T) {
	// Walks a directory that doesn't exist — must not error.
	err := jobs.BackfillRunResults("/nonexistent/jobs/dir")
	assert.NoError(t, err)
}

func TestBackfillRunResults_NoDuckDB(t *testing.T) {
	// Job dir present but no projection-audit/projection.duckdb — must
	// silently skip without error.
	jobsDir := t.TempDir()
	require.NoError(t, os.MkdirAll(filepath.Join(jobsDir, "empty-job"), 0755))
	err := jobs.BackfillRunResults(jobsDir)
	assert.NoError(t, err)
}
