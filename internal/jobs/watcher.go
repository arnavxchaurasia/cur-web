package jobs

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
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
	"github.com/facets/cur-web/internal/storage"
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
	storage storage.Client
}

func NewWatcher(database *db.DB, spawner *Spawner, jobsDir string, nc NotifyConfig) *Watcher {
	return &Watcher{db: database, spawner: spawner, jobsDir: jobsDir, notify: nc, storage: &storage.NoopClient{}}
}

func (w *Watcher) SetStorage(s storage.Client) {
	if s != nil {
		w.storage = s
	}
}

func (w *Watcher) syncRemote(jobID, jobDir string) {
	if w.storage != nil && w.storage.IsConfigured() {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
			defer cancel()
			if err := w.storage.SyncJobToRemote(ctx, jobID, jobDir); err != nil {
				slog.Warn("storage sync failed", "job_id", jobID, "err", err)
			}
		}()
	}
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

	// §2 Make finalize cancel-aware (and STOP_ALL aware):
	// Check before success/failure/retry branches.
	stopAllActive := IsStopAllActive(filepath.Dir(w.jobsDir))
	if job.Status == "cancelled" || IsCancelled(jobDir) || stopAllActive {
		// Authoritative stop. Do not retry, do not mark done/failed.
		if job.AgentPID > 0 {
			killGroup(job.AgentPID) // reap any survivors
		}
		_ = os.Remove(filepath.Join(jobDir, "orchestrator.lock"))
		if job.Status != "cancelled" {
			_ = w.db.UpdateJobCancelled(jobID)
			metrics.JobsCancelled.Add(1)
		}
		log.Printf("watcher %s: cancel observed (stopAll=%v) — not resuming", jobID, stopAllActive)
		return
	}

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
		outcome, vsummary := runValidatorGate(jobDir)
		// The report is always served (see WORKORDER §0 P1). This only keeps the
		// verdict: persist it next to the report so the team can tell afterwards
		// whether the check was clean, reported violations, or never executed.
		// Internal only — not surfaced to the customer or the job status.
		writeValidationVerdict(jobDir, outcome, vsummary)
		switch outcome {
		case validatorFailed:
			slog.Warn("validator reported violations", "job_id", jobID, "summary", vsummary)
			metrics.ValidationViolations.Add(1)
		case validatorNotRun:
			slog.Warn("validator did not execute", "job_id", jobID, "detail", vsummary)
			metrics.ValidationNotRun.Add(1)
		default:
			slog.Info("validator clean", "job_id", jobID)
		}
		var gcpOD *float64
		latestRunID := "initial"
		if runs, err := QueryRunResults(dbPath); err == nil && len(runs) > 0 {
			gcpOD = runs[0].GCPOD
			if runs[0].RunID != "" {
				latestRunID = runs[0].RunID
			}
		}
		if err := w.db.UpdateJobDoneWithGCPOD(jobID, spend, gcpOD); err != nil {
			log.Printf("watcher %s: UpdateJobDoneWithGCPOD: %v", jobID, err)
		}
		w.recordEquivalenceUsage(jobID, jobDir)
		w.recordReviewFindings(jobID, jobDir)
		metrics.JobsDone.Add(1)
		slog.Info("job done", "job_id", jobID, "prospect", job.Prospect, "aws_spend", spend, "gcp_od", gcpOD, "attempts", job.Attempts)

		// Persist run_diagnostics.json summary into the durable DB so it
		// survives job-directory expiration (ephemeral storage redeploys).
		diagPath := filepath.Join(jobDir, "projection-audit", "run_diagnostics.json")
		if summary, err := os.ReadFile(diagPath); err == nil {
			if err := w.db.SaveJobDiagnostics(jobID, latestRunID, summary); err != nil {
				log.Printf("watcher %s: SaveJobDiagnostics: %v", jobID, err)
			}
		} else {
			log.Printf("watcher %s: run_diagnostics.json not present, skipping diagnostics persist", jobID)
		}
		w.recordJobLogDigest(jobID, latestRunID, jobDir)
		w.syncRemote(jobID, jobDir)
		go func() {
			userSettings, err := w.db.GetNotificationSettings(job.Owner)
			emailEnabled := true
			slackEnabled := false
			slackURL := w.slackURL()
			slackChannel := ""
			if err == nil && userSettings != nil {
				emailEnabled = userSettings.EmailNotifications
				slackEnabled = userSettings.SlackNotifications
				if userSettings.SlackWebhookURL != "" {
					slackURL = userSettings.SlackWebhookURL
				}
				slackChannel = userSettings.SlackChannel
			}

			// Send email if user has email notifications on (default true)
			if emailEnabled {
				if err := notify.SendJobReady(w.emailCfg(), job.Owner, job.Owner, job.Prospect, jobID, spend); err != nil {
					slog.Info("email notification status", "job_id", jobID, "detail", err.Error())
				}
			}

			// Send Slack notification if enabled and webhook exists
			if (slackEnabled || w.slackURL() != "") && slackURL != "" {
				prefix := ""
				if slackChannel != "" {
					prefix = fmt.Sprintf("[%s] ", slackChannel)
				}
				if err := notify.PostJobSuccess(slackURL, prefix+job.Prospect, job.Owner, jobID, spend); err != nil {
					slog.Warn("PostJobSuccess failed", "job_id", jobID, "err", err)
				}
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
		w.recordJobLogDigest(jobID, "initial", jobDir)
		w.syncRemote(jobID, jobDir)
		go func() {
			if err := notify.SendJobFailed(w.emailCfg(), job.Owner, job.Owner, job.Prospect); err != nil {
				slog.Warn("SendJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobFailed(w.slackURL(), job.Prospect, job.Owner, jobID); err != nil {
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
		w.syncRemote(jobID, jobDir)
		go func() {
			if err := notify.SendJobFailed(w.emailCfg(), job.Owner, job.Owner, job.Prospect); err != nil {
				slog.Warn("SendJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobFailed(w.slackURL(), job.Prospect, job.Owner, jobID); err != nil {
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
		w.recordJobLogDigest(jobID, "initial", jobDir)
		w.syncRemote(jobID, jobDir)
		go func() {
			if err := notify.SendJobFailed(w.emailCfg(), job.Owner, job.Owner, job.Prospect); err != nil {
				slog.Warn("SendJobFailed failed", "job_id", jobID, "err", err)
			}
		}()
		go func() {
			if err := notify.PostJobFailed(w.slackURL(), job.Prospect, job.Owner, jobID); err != nil {
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

	_ = os.Remove(filepath.Join(jobDir, "orchestrator.lock"))

	pid, assumptionsVersion, err := w.spawner.Start(jobDir, job.InputExt, "", job.Engine)
	if err != nil {
		w.db.UpdateJobFailed(jobID, "retry spawn failed: "+err.Error())
		return
	}
	if assumptionsVersion != "" {
		_ = w.db.UpdateJobAssumptionsVersion(jobID, assumptionsVersion)
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
	// Keep enough room for a captured Python traceback tail (folded into
	// failure.txt by the orchestrator) so the real error survives in the DB —
	// on Render's free tier agy.log is wiped on spin-down, so this persisted
	// reason is the only durable copy.
	if runes := []rune(s); len(runes) > 6000 {
		s = string(runes[:6000]) + "…"
	}
	return s
}

// IsCancelled returns true if <jobDir>/cancelled sentinel file exists.
func IsCancelled(jobDir string) bool {
	_, err := os.Stat(filepath.Join(filepath.Clean(jobDir), "cancelled"))
	return err == nil
}

// WriteCancelledSentinel writes the <jobDir>/cancelled sentinel file.
func WriteCancelledSentinel(jobDir, canceller, reason string) error {
	content := fmt.Sprintf("cancelled by %s at %s: %s\n", canceller, time.Now().UTC().Format(time.RFC3339), reason)
	return os.WriteFile(filepath.Join(filepath.Clean(jobDir), "cancelled"), []byte(content), 0644)
}

// IsStopAllActive returns true if the global STOP_ALL sentinel exists in dataDir.
func IsStopAllActive(dataDir string) bool {
	if dataDir == "" {
		return false
	}
	_, err := os.Stat(filepath.Join(filepath.Clean(dataDir), "STOP_ALL"))
	return err == nil
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


// ValidatorOutcome describes what actually happened when the gate ran.
// "could not run" is deliberately distinct from "passed": in that case the
// check produced no result at all, which the team needs to be able to see.
type ValidatorOutcome int

const (
	ValidatorClean  ValidatorOutcome = iota // exit 0
	ValidatorFailed                         // exit 1 or 2 — violations reported
	ValidatorNotRun                         // could not execute at all
)

type validatorOutcome = ValidatorOutcome

const (
	validatorClean  = ValidatorClean
	validatorFailed = ValidatorFailed
	validatorNotRun = ValidatorNotRun
)

func runValidatorGate(jobDir string) (ValidatorOutcome, string) {
	script := filepath.Join(skill.ResolveDir(), "scripts", "validate_fix.py")
	if _, err := os.Stat(script); err != nil {
		return ValidatorNotRun, "validator script absent at " + script
	}
	// Autofix pass first (regional SKU repair, per-N clamping, CUD synthesis).
	if err := exec.Command(pythonBin(), script, jobDir).Run(); err != nil {
		log.Printf("autofix pass failed for %s (non-fatal): %v", jobDir, err)
	}
	out, err := exec.Command(pythonBin(), script, "--check-only", jobDir).CombinedOutput()
	summary := strings.TrimSpace(string(out))
	if err != nil {
		if _, ok := err.(*exec.ExitError); ok {
			return ValidatorFailed, summary
		}
		return ValidatorNotRun, "validator could not execute (" + err.Error() + "): " + summary
	}
	return ValidatorClean, summary
}

// RunValidatorGate exports runValidatorGate for testing.
func RunValidatorGate(jobDir string) (ValidatorOutcome, string) {
	return runValidatorGate(jobDir)
}

// WriteValidationVerdict records the gate outcome as a file in the job dir so
// the verdict survives log rotation. Internal artifact; nothing reads it
// automatically.
func WriteValidationVerdict(jobDir string, outcome ValidatorOutcome, summary string) {
	label := "clean"
	switch outcome {
	case ValidatorFailed:
		label = "violations"
	case ValidatorNotRun:
		label = "not_run"
	}
	body := label + "\n" + time.Now().UTC().Format(time.RFC3339) + "\n\n" + summary + "\n"
	path := filepath.Join(filepath.Clean(jobDir), "validation_verdict.txt")
	if err := os.WriteFile(path, []byte(body), 0644); err != nil {
		log.Printf("writeValidationVerdict: %v", err)
	}
}

func writeValidationVerdict(jobDir string, outcome ValidatorOutcome, summary string) {
	WriteValidationVerdict(jobDir, outcome, summary)
}

func (w *Watcher) recordEquivalenceUsage(jobID, jobDir string) {
	usageFile := filepath.Join(jobDir, "projection-audit", "equivalence_usage.json")
	if _, err := os.Stat(usageFile); os.IsNotExist(err) {
		usageFile = filepath.Join(jobDir, "equivalence_usage.json")
		if _, err := os.Stat(usageFile); os.IsNotExist(err) {
			return
		}
	}

	if w.db == nil {
		diagMsg := fmt.Sprintf("DIAGNOSTIC: Durable database is unconfigured; cannot record equivalence usage for job %s\n", jobID)
		slog.Warn("equivalence usage tracking skipped: durable database is unconfigured", "job_id", jobID)
		auditDir := filepath.Join(jobDir, "projection-audit")
		_ = os.MkdirAll(auditDir, 0755)
		_ = os.WriteFile(filepath.Join(auditDir, "equivalence_diagnostic.txt"), []byte(diagMsg), 0644)
		return
	}

	data, err := os.ReadFile(usageFile)
	if err != nil {
		slog.Warn("failed to read equivalence_usage.json", "job_id", jobID, "err", err)
		return
	}

	var records []db.EquivalenceUsageRecord
	if err := json.Unmarshal(data, &records); err != nil {
		slog.Warn("failed to unmarshal equivalence_usage.json", "job_id", jobID, "err", err)
		return
	}

	for i := range records {
		if records[i].JobID == "" {
			records[i].JobID = jobID
		}
		if records[i].RecordedAt == "" {
			records[i].RecordedAt = time.Now().UTC().Format(time.RFC3339)
		}
	}

	if err := w.db.RecordEquivalenceUsage(records); err != nil {
		slog.Error("failed to record equivalence usage to db", "job_id", jobID, "err", err)
	} else {
		slog.Info("recorded equivalence usage to db", "job_id", jobID, "count", len(records))
	}
}

func (w *Watcher) recordReviewFindings(jobID, jobDir string) {
	findingsFile := filepath.Join(jobDir, "projection-audit", "review_findings.json")
	if _, err := os.Stat(findingsFile); os.IsNotExist(err) {
		findingsFile = filepath.Join(jobDir, "review_findings.json")
		if _, err := os.Stat(findingsFile); os.IsNotExist(err) {
			return
		}
	}

	if w.db == nil {
		return
	}

	data, err := os.ReadFile(findingsFile)
	if err != nil {
		slog.Warn("failed to read review_findings.json", "job_id", jobID, "err", err)
		return
	}

	var rawFindings []struct {
		JobID            string         `json:"job_id"`
		AWSLIKey         string         `json:"aws_li_key"`
		CanonicalService string         `json:"canonical_service"`
		ChargeType       string         `json:"charge_type"`
		FindingType      string         `json:"finding_type"`
		Severity         string         `json:"severity"`
		Evidence         map[string]any `json:"evidence"`
		SuggestedAction  string         `json:"suggested_action"`
	}

	if err := json.Unmarshal(data, &rawFindings); err != nil {
		slog.Warn("failed to unmarshal review_findings.json", "job_id", jobID, "err", err)
		return
	}

	var dbFindings []db.ReviewFinding
	for _, rf := range rawFindings {
		jid := rf.JobID
		if jid == "" {
			jid = jobID
		}
		var awsOD, gcpOD, ratio float64
		if rf.Evidence != nil {
			if v, ok := rf.Evidence["aws_od"].(float64); ok {
				awsOD = v
			}
			if v, ok := rf.Evidence["gcp_od"].(float64); ok {
				gcpOD = v
			}
			if v, ok := rf.Evidence["ratio"].(float64); ok {
				ratio = v
			}
		}
		evBytes, _ := json.Marshal(rf.Evidence)

		dbFindings = append(dbFindings, db.ReviewFinding{
			JobID:            jid,
			AWSLIKey:         rf.AWSLIKey,
			CanonicalService: rf.CanonicalService,
			ChargeType:       rf.ChargeType,
			FindingType:      rf.FindingType,
			Severity:         rf.Severity,
			AWSOD:            awsOD,
			GCPOD:            gcpOD,
			Ratio:            ratio,
			EvidenceJSON:     string(evBytes),
			Evidence:         rf.Evidence,
			SuggestedAction:  rf.SuggestedAction,
			Status:           "pending",
		})
	}

	if err := w.db.SaveReviewFindings(dbFindings); err != nil {
		slog.Error("failed to save review findings to db", "job_id", jobID, "err", err)
	} else {
		slog.Info("saved review findings to db", "job_id", jobID, "count", len(dbFindings))
	}
}

func (w *Watcher) recordJobLogDigest(jobID, runID, jobDir string) {
	if w.db == nil {
		return
	}
	logPath := filepath.Join(jobDir, "projection-audit", "debug.log")
	if _, err := os.Stat(logPath); os.IsNotExist(err) {
		logPath = filepath.Join(jobDir, "agy.log")
		if _, err := os.Stat(logPath); os.IsNotExist(err) {
			slog.Debug("watcher: no log file for digest", "job_id", jobID)
			return
		}
	}

	f, err := os.Open(logPath)
	if err != nil {
		slog.Debug("watcher: open log file for digest failed", "job_id", jobID, "err", err)
		return
	}
	defer f.Close()

	var allLines []string
	var errCount, warnCount int
	var firstError string

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		text := sc.Text()
		allLines = append(allLines, text)
		upper := strings.ToUpper(text)
		if strings.Contains(upper, " ERROR ") || strings.Contains(upper, "CRITICAL") || strings.HasPrefix(upper, "ERROR:") {
			errCount++
			if firstError == "" {
				firstError = text
			}
		} else if strings.Contains(upper, " WARN ") || strings.Contains(upper, "WARNING") {
			warnCount++
		}
	}

	lastLines := ""
	if len(allLines) > 50 {
		lastLines = strings.Join(allLines[len(allLines)-50:], "\n")
	} else {
		lastLines = strings.Join(allLines, "\n")
	}

	digest := db.JobLogDigest{
		JobID:      jobID,
		RunID:      runID,
		ErrorCount: errCount,
		WarnCount:  warnCount,
		FirstError: firstError,
		LastLines:  lastLines,
	}

	if err := w.db.SaveJobLogDigest(digest); err != nil {
		slog.Warn("watcher: SaveJobLogDigest failed", "job_id", jobID, "err", err)
	} else {
		slog.Info("saved job log digest", "job_id", jobID, "errors", errCount, "warns", warnCount)
	}
}
