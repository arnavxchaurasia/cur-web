package jobs

import (
	"io"
	"log"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/facets/cur-web/internal/config"
	"github.com/facets/cur-web/internal/db"
	"github.com/facets/cur-web/internal/metrics"
	"github.com/facets/cur-web/internal/notify"
	"github.com/facets/cur-web/internal/skill"
)

const (
	agyInternalLog = "agy-internal.log"
	maxAttempts    = 3
	pollInterval   = 5 * time.Second
	finalizeWait   = 2 * time.Second
	// staleTimeout: AGY writes nothing to agy.log while waiting for the model
	// API response (can take 20-40 min on large bills). Set generously so the
	// watcher never kills a job that's genuinely waiting on the API.
	// Derived from the same constant as agy's own "--print-timeout" (baseFlags,
	// spawner.go) so the two can never drift — if staleTimeout < agy's timeout,
	// the watcher would kill a live job early.
	staleTimeout = config.AGYTimeoutMinutes * time.Minute
)

// Watcher polls a job's agy PID until it dies, then finalizes (mark done,
// retry up to maxAttempts, or mark failed). One Watch goroutine per job —
// either kicked off by the handler when a new job is created, or attached at
// startup to orphans surviving a backend restart.
type Watcher struct {
	db      *db.DB
	spawner *Spawner
	jobsDir string
	notify  NotifyConfig
}

func NewWatcher(database *db.DB, spawner *Spawner, jobsDir string, nc NotifyConfig) *Watcher {
	return &Watcher{db: database, spawner: spawner, jobsDir: jobsDir, notify: nc}
}

// Watch blocks until the job reaches a terminal state. Used for fresh runs
// that may clean-slate retry up to maxAttempts on failure.
func (w *Watcher) Watch(jobID string, pid int) {
	logOffset := logSize(filepath.Join(w.jobsDir, jobID, agyInternalLog))
	w.watchUntilDead(jobID, pid)
	w.finalizeWithOffset(jobID, true, logOffset)
}

// WatchOnce blocks until the process dies, then finalizes WITHOUT retry.
// Used for refinement runs — failure must not wipe the original successful
// projection state.
func (w *Watcher) WatchOnce(jobID string, pid int) {
	logOffset := logSize(filepath.Join(w.jobsDir, jobID, agyInternalLog))
	w.watchUntilDead(jobID, pid)
	w.finalizeWithOffset(jobID, false, logOffset)
}

// logSize returns the current byte size of a file, 0 if missing.
func logSize(path string) int64 {
	if fi, err := os.Stat(path); err == nil {
		return fi.Size()
	}
	return 0
}

// watchUntilDead is the shared polling loop. Two liveness signals each tick:
//   - Process liveness via pidAlive (rejects zombies + missing pids).
//   - Activity freshness: the newer of agy.log (continuous stdout/stderr)
//     and progress.json (rewritten by the skill at each phase transition). A
//     long phase that goes quiet on stdout still advances progress.json, and a
//     burst of stdout with no phase change still bumps agy.log — taking the
//     max of both avoids SIGKILLing a job that's genuinely still working. If
//     neither has advanced in staleTimeout the process is considered hung and
//     we SIGKILL the group so the next tick sees it gone.
func (w *Watcher) watchUntilDead(jobID string, pid int) {
	jobDir := filepath.Clean(filepath.Join(w.jobsDir, jobID))
	// Per-watch activity watermark. Initialized to spawn time (now) so a
	// pre-existing log from a prior run doesn't false-trip stale detection
	// in the first few seconds.
	lastActivity := time.Now()
	for pidAlive(pid) {
		if m := w.activityMtime(jobDir); m.After(lastActivity) {
			lastActivity = m
		}
		if time.Since(lastActivity) > staleTimeout {
			log.Printf("watcher %s: no log activity for >%s, killing pid %d", jobID, staleTimeout, pid)
			killGroup(pid)
		}
		time.Sleep(pollInterval)
	}
}

// activityMtime returns the most recent mtime among the job's liveness
// signals — agy.log and progress.json. Returns zero-time if neither file
// exists yet (agy hasn't produced output and the skill hasn't written its
// first phase marker).
func (w *Watcher) activityMtime(jobDir string) time.Time {
	var newest time.Time
	for _, name := range []string{"agy.log", agyInternalLog, progressFile, "run_all.py"} {
		info, err := os.Stat(filepath.Join(jobDir, name))
		if err != nil {
			continue
		}
		if m := info.ModTime(); m.After(newest) {
			newest = m
		}
	}
	return newest
}


func (w *Watcher) finalizeWithOffset(jobID string, allowRetry bool, logOffset int64) {
	w.finalize(jobID, allowRetry, logOffset)
}

func (w *Watcher) finalize(jobID string, allowRetry bool, logOffset int64) {
	// Brief grace period in case agy was just finishing the report write.
	time.Sleep(finalizeWait)

	job, err := w.db.GetJob(jobID)
	if err != nil {
		log.Printf("watcher %s: GetJob: %v", jobID, err)
		return
	}
	jobDir := filepath.Clean(filepath.Join(w.jobsDir, jobID))

	// SUCCESS CHECK FIRST: report presence is the ground truth. If render_report.py
	// ran and wrote an HTML file, the job succeeded — regardless of what agy logged
	// (quota errors from prior attempts, sub-agent noise, etc.). Checking logs before
	// the report caused healthy jobs to be marked failed when old quota markers were
	// still present in an append-only agy-internal.log.
	reportPath, _ := resolveReportPath(jobDir, "")
	if reportPath != "" {
		// Source aws_spend from the duckdb (canonical) and fall back to
		// the HTML regex one release as a safety net for jobs whose
		// duckdb shape diverges from the current skill version.
		dbPath := filepath.Join(jobDir, "projection-audit", "projection.duckdb")
		spend, err := QueryAWSSpend(dbPath)
		if err != nil {
			log.Printf("watcher %s: QueryAWSSpend: %v (will try html fallback)", jobID, err)
		}
		if spend == 0 {
			// Safety net for one release: parse the legacy
			// <div id="aws-total-spend">...</div> element from the
			// HTML report. Drop this once every job has a populated
			// projection.duckdb / aws_li_catalog table.
			if content, err := os.ReadFile(reportPath); err == nil {
				spend = extractAWSSpendFromHTML(string(content))
			}
		}
		// Run the validator gate for audit/logging. Violations are logged as
		// warnings but do NOT block the report — a report on disk is always served.
		if ok, summary := runValidatorGate(jobDir); !ok {
			slog.Warn("validation gate violations (report still served)", "job_id", jobID, "summary", summary)
		}
		if err := w.db.UpdateJobDone(jobID, spend); err != nil {
			log.Printf("watcher %s: UpdateJobDone: %v", jobID, err)
		}
		metrics.JobsDone.Add(1)
		slog.Info("job done", "job_id", jobID, "prospect", job.Prospect, "aws_spend", spend, "attempts", job.Attempts)
		go func() {
			if err := notify.SendJobReady(w.notify.Email, job.Owner, job.Owner, job.Prospect, jobID); err != nil {
				slog.Warn("SendJobReady failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobSuccess(w.notify.SlackURL, job.Prospect, job.Owner, jobID, spend); err != nil {
				slog.Warn("PostJobSuccess failed", "job_id", jobID, "err", err)
			}
		}()
		return
	}

	// No report produced. Check for explicit failure signals before retrying.

	// Explicit structural-failure path: if the orchestrator wrote failure.txt, it has
	// diagnosed the input as unprocessable (wrong cloud, corrupted file, unknown shape).
	// Surface that reason cleanly — retrying the same broken input burns attempts.
	if reason := readFailureReason(jobDir); reason != "" {
		if err := w.db.UpdateJobFailed(jobID, reason); err != nil {
			log.Printf("watcher %s: UpdateJobFailed (failure.txt): %v", jobID, err)
		}
		metrics.JobsFailed.Add(1)
		slog.Error("job failed", "job_id", jobID, "prospect", job.Prospect, "reason", reason, "attempts", job.Attempts)
		go func() {
			if err := notify.SendJobFailed(w.notify.Email, job.Owner, job.Owner, job.Prospect); err != nil {
				slog.Warn("SendJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobFailed(w.notify.SlackURL, job.Prospect, job.Owner, jobID); err != nil {
				slog.Warn("PostJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		return
	}

	// Quota fail-fast: only scan bytes written by this process (logOffset). A prior
	// run's quota error in the append-only log will NOT match here.
	if quotaExhausted(jobDir, logOffset) {
		if err := w.db.UpdateJobFailed(jobID, "Gemini API quota exhausted — wait for quota reset or switch API key"); err != nil {
			log.Printf("watcher %s: UpdateJobFailed (quota): %v", jobID, err)
		}
		metrics.JobsFailed.Add(1)
		slog.Error("job failed", "job_id", jobID, "prospect", job.Prospect, "reason", "quota_exhausted", "attempts", job.Attempts)
		go func() {
			if err := notify.SendJobFailed(w.notify.Email, job.Owner, job.Owner, job.Prospect); err != nil {
				slog.Warn("SendJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobFailed(w.notify.SlackURL, job.Prospect, job.Owner, jobID); err != nil {
				slog.Warn("PostJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		return
	}

	if !allowRetry || job.Attempts >= maxAttempts {
		tail := tailLog(filepath.Join(jobDir, "agy.log"), 500)
		var errMsg string
		if !allowRetry {
			errMsg = "refinement run exited without updating report.html"
		} else {
			errMsg = "agy exited without report.html after " + strconv.Itoa(job.Attempts) + " attempts"
		}
		if tail != "" {
			errMsg += "; tail: " + tail
		}
		if err := w.db.UpdateJobFailed(jobID, errMsg); err != nil {
			log.Printf("watcher %s: UpdateJobFailed: %v", jobID, err)
		}
		metrics.JobsFailed.Add(1)
		slog.Error("job failed", "job_id", jobID, "prospect", job.Prospect, "reason", errMsg, "attempts", job.Attempts)
		go func() {
			if err := notify.SendJobFailed(w.notify.Email, job.Owner, job.Owner, job.Prospect); err != nil {
				slog.Warn("SendJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobFailed(w.notify.SlackURL, job.Prospect, job.Owner, jobID); err != nil {
				slog.Warn("PostJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		return
	}

	// Retry: resume from where the orchestrator left off. phase_checkpoint.json
	// records the last successfully completed phase; the new orchestrator process
	// reads it and starts from the next phase — no data wipe, no Phase 1 redo.
	metrics.JobsRetried.Add(1)
	slog.Warn("job retrying", "job_id", jobID, "prospect", job.Prospect, "attempt", job.Attempts+1, "max_attempts", maxAttempts)
	log.Printf("watcher %s: checkpoint retry (attempt %d/%d)", jobID, job.Attempts+1, maxAttempts)
	w.db.IncrementJobAttempts(jobID)
	w.db.UpdateJobRunning(jobID)

	pid, err := w.spawner.Start(jobDir, job.InputExt, "")
	if err != nil {
		w.db.UpdateJobFailed(jobID, "retry spawn failed: "+err.Error())
		return
	}
	w.db.UpdateJobPID(jobID, pid)
	w.Watch(jobID, pid)
}

// readFailureReason returns the trimmed contents of <jobDir>/failure.txt, or
// "" if the file is absent/empty. agy writes this when it detects the
// input is structurally unprocessable (wrong cloud, corrupted, etc.) so the
// watcher can mark the job failed with a useful message instead of burning
// three retry attempts on the same broken input.
func readFailureReason(jobDir string) string {
	data, err := os.ReadFile(filepath.Join(filepath.Clean(jobDir), "failure.txt"))
	if err != nil {
		return ""
	}
	s := strings.TrimSpace(string(data))
	if runes := []rune(s); len(runes) > 1000 {
		s = string(runes[:1000]) + "…"
	}
	return s
}

// quotaMarkers are substrings (case-insensitive) that indicate the Gemini API
// quota was exhausted. Checked against up to 16 KB of agy-internal.log tail so
// the watcher can fail fast without burning retry attempts on a quota error.
// quotaMarkers must be specific enough to not match benign log lines.
// "quota" alone is too broad — it matches log lines like "API quota usage: 0/60".
// Kept in sync with QUOTA_MARKERS in orchestrate.go so an auth failure
// (PERMISSION_DENIED / UNAUTHENTICATED) is treated as fail-fast here too,
// instead of being mislabeled "exited without report" and burning all retries.
// quotaMarkers are permanent / unrecoverable signals only. RESOURCE_EXHAUSTED
// and rate-limit variants (429, "rate limit exceeded", "Too Many Requests") are
// excluded: they fire on transient RPM/TPM spikes and would stop retry on a
// job that would succeed on the next attempt. Only markers that mean "every
// future API call will also fail" are listed here.
var quotaMarkers = []string{
	"Individual quota reached",
	"quota exhausted",
	"model unreachable",
	"PERMISSION_DENIED",
	"UNAUTHENTICATED",
}

func quotaExhausted(jobDir string, logOffset int64) bool {
	logPath := filepath.Join(filepath.Clean(jobDir), agyInternalLog)
	f, err := os.Open(logPath)
	if err != nil {
		return false
	}
	defer f.Close()
	// Only read bytes written after logOffset so retries don't see old errors.
	if logOffset > 0 {
		if _, err := f.Seek(logOffset, 0); err != nil {
			return false
		}
	}
	const maxTailBytes = 16 * 1024
	data, err := io.ReadAll(io.LimitReader(f, maxTailBytes))
	if err != nil {
		return false
	}
	lower := strings.ToLower(string(data))
	for _, marker := range quotaMarkers {
		if strings.Contains(lower, strings.ToLower(marker)) {
			return true
		}
	}
	return false
}

// awsSpendRe matches the legacy total-spend element rendered by older
// versions of the projection report. Kept inline here (not exported)
// purely as a one-release safety net while QueryAWSSpend rolls out.
var awsSpendRe = regexp.MustCompile(`id="aws-total-spend"[^>]*>([0-9]+(?:\.[0-9]+)?)<`)

func extractAWSSpendFromHTML(html string) float64 {
	m := awsSpendRe.FindStringSubmatch(html)
	if len(m) < 2 {
		return 0
	}
	v, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		return 0
	}
	return v
}

// PidAlive is the exported equivalent of pidAlive for use in cmd/server.
func PidAlive(pid int) bool { return pidAlive(pid) }

// pidAlive reports whether pid is a *running* process (not zombie/defunct).
// kill(pid, 0) returns nil for both running and zombie processes, so we also
// query `ps -o stat` and treat a state starting with 'Z' as dead.


// runValidatorGate runs the deterministic projection validator in check-only
// mode against the job's projection.duckdb. Returns (passed, summary).
//
// Policy: fail-CLOSED on validator violations (exit 1) and on a malformed
// projection (exit 2) — a report with hard violations must not be served as
// "done". Fail-OPEN only when the validator literally cannot run (python or
// the script missing), so an environment problem never blocks a good job.
func runValidatorGate(jobDir string) (bool, string) {
	script := filepath.Join(skill.ResolveDir(), "scripts", "validate_fix.py")
	if _, err := os.Stat(script); err != nil {
		return true, "validator script absent; gate skipped"
	}
	// First run in autofix mode to automatically repair minor errors (e.g. regional SKU mismatches, missing CUD rates, per-N clamping)
	if err := exec.Command("python3", script, jobDir).Run(); err != nil {
		log.Printf("autofix pass failed for %s (non-fatal): %v", jobDir, err)
	}

	out, err := exec.Command("python3", script, "--check-only", jobDir).CombinedOutput()
	summary := strings.TrimSpace(string(out))
	if err != nil {
		// Non-zero exit (violations or malformed db) -> fail closed.
		if _, ok := err.(*exec.ExitError); ok {
			return false, summary
		}
		// Could not start python at all -> fail open (env issue, not a data issue).
		return true, "validator could not run (" + err.Error() + "); gate skipped"
	}
	return true, summary
}
