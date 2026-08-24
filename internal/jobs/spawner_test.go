package jobs_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/facets/cur-web/internal/jobs"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fakeAGY records its args, writes a minimal report.html, and exits 0
const fakeAGY = `#!/bin/bash
echo "$@" > args.txt
echo "fake agy running"
echo '<div id="aws-total-spend">12345.00</div>' > report.html
exit 0`

func TestSpawner_Success(t *testing.T) {
	dir := t.TempDir()
	jobDir := filepath.Join(dir, "job-1")
	os.MkdirAll(jobDir, 0755)
	os.WriteFile(filepath.Join(jobDir, "input.csv"), []byte("header,data"), 0644)

	agyPath := filepath.Join(dir, "agy")
	os.WriteFile(agyPath, []byte(fakeAGY), 0755)

	s := jobs.NewSpawner(jobs.SpawnerConfig{
		AGYPath: agyPath,
	})

	result := s.Run(jobDir, ".csv")
	require.NoError(t, result.Err)
	assert.InDelta(t, 12345.0, result.AWSSpend, 0.01)

	_, err := os.Stat(filepath.Join(jobDir, "report.html"))
	assert.NoError(t, err)
}

// AGY does not accept a caller-supplied session ID for fresh starts
// (--conversation is resume-only). Verify the session ID is accepted by the
// API but not forwarded to the agy binary as --conversation.
func TestSpawner_SessionIDNotForwardedOnFreshRun(t *testing.T) {
	dir := t.TempDir()
	jobDir := filepath.Join(dir, "job-sid")
	os.MkdirAll(jobDir, 0755)
	os.WriteFile(filepath.Join(jobDir, "input.csv"), []byte("header,data"), 0644)

	agyPath := filepath.Join(dir, "agy")
	os.WriteFile(agyPath, []byte(fakeAGY), 0755)

	s := jobs.NewSpawner(jobs.SpawnerConfig{AGYPath: agyPath})

	const sid = "11111111-2222-3333-4444-555555555555"
	result := s.RunWithSession(jobDir, ".csv", sid)
	require.NoError(t, result.Err)

	args, err := os.ReadFile(filepath.Join(jobDir, "args.txt"))
	require.NoError(t, err)
	assert.NotContains(t, string(args), "--conversation")
	assert.NotContains(t, string(args), sid)
}

func TestSpawner_NoConversationFlagOnFreshRun(t *testing.T) {
	dir := t.TempDir()
	jobDir := filepath.Join(dir, "job-nosid")
	os.MkdirAll(jobDir, 0755)
	os.WriteFile(filepath.Join(jobDir, "input.csv"), []byte("header,data"), 0644)

	agyPath := filepath.Join(dir, "agy")
	os.WriteFile(agyPath, []byte(fakeAGY), 0755)

	s := jobs.NewSpawner(jobs.SpawnerConfig{AGYPath: agyPath})

	result := s.Run(jobDir, ".csv")
	require.NoError(t, result.Err)

	args, err := os.ReadFile(filepath.Join(jobDir, "args.txt"))
	require.NoError(t, err)
	assert.NotContains(t, string(args), "--conversation")
}

func TestSpawner_Failure(t *testing.T) {
	dir := t.TempDir()
	jobDir := filepath.Join(dir, "job-fail")
	os.MkdirAll(jobDir, 0755)

	agyPath := filepath.Join(dir, "agy-fail")
	os.WriteFile(agyPath, []byte("#!/bin/bash\necho 'error' >&2\nexit 1"), 0755)

	s := jobs.NewSpawner(jobs.SpawnerConfig{
		AGYPath: agyPath,
	})

	result := s.Run(jobDir, ".csv")
	assert.Error(t, result.Err)
}
