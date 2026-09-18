// internal/jobs/facets.go
//
// Internal-only panel for @facets.cloud employees: view recent job logs and
// generate a consolidated multi-job summary report (AWS Total / AWS Mapped
// Cost / GCP On-Demand / Diff, per customer and per category), matching the
// format of the original reference workbook's "Summary" tab. Not linked from
// any customer-facing page — reached only via the hidden floating badge the
// frontend renders exclusively for @facets.cloud sessions. Every handler
// here re-checks auth.Session.IsFacetsEmployee() itself (never trust the
// frontend's decision to show or hide the badge as the real gate).
package jobs

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/skill"
	"github.com/go-chi/chi/v5"
)

// portfolioSummaryCooldown throttles FacetsPortfolioSummary per session —
// it shells out to a Python subprocess that opens every selected job's
// duckdb file, so a caller mashing the button (or a buggy client retrying
// in a loop) could otherwise pile up concurrent subprocesses. This is an
// internal, low-traffic manual tool, not customer-facing, so a simple
// in-memory per-session cooldown is enough — no need for a rate-limiting
// dependency or persistent store.
var (
	portfolioSummaryMu       sync.Mutex
	portfolioSummaryLastCall = map[string]time.Time{}
)

const portfolioSummaryCooldown = 5 * time.Second

// checkPortfolioSummaryCooldown reports whether `email` may proceed now.
// When it returns false, the second value is how much longer to wait.
func checkPortfolioSummaryCooldown(email string) (time.Duration, bool) {
	portfolioSummaryMu.Lock()
	defer portfolioSummaryMu.Unlock()
	now := time.Now()
	if last, ok := portfolioSummaryLastCall[email]; ok {
		if elapsed := now.Sub(last); elapsed < portfolioSummaryCooldown {
			return portfolioSummaryCooldown - elapsed, false
		}
	}
	portfolioSummaryLastCall[email] = now
	return 0, true
}

func requireFacetsEmployee(w http.ResponseWriter, r *http.Request) bool {
	sess := auth.SessionFromCtx(r.Context())
	if !sess.IsFacetsEmployee() {
		http.Error(w, `{"error":"forbidden"}`, http.StatusForbidden)
		return false
	}
	return true
}

// FacetsListJobs lists every job across every owner, same data as
// AdminListAll but gated on IsFacetsEmployee instead of IsAdmin — the two
// grants are independent (see IsFacetsEmployee's doc comment).
func (h *Handler) FacetsListJobs(w http.ResponseWriter, r *http.Request) {
	if !requireFacetsEmployee(w, r) {
		return
	}
	jobs, err := h.db.ListAllJobs()
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	if jobs == nil {
		w.Write([]byte("[]"))
		return
	}
	json.NewEncoder(w).Encode(jobs)
}

// FacetsLogs returns the tail of one job's orchestrator log (agy.log) — the
// same file the terminal orchestrator writes phase-by-phase progress and
// script stdout/stderr to. `lines` query param caps how many trailing lines
// are returned (default 200, capped at 2000 to keep the response bounded).
func (h *Handler) FacetsLogs(w http.ResponseWriter, r *http.Request) {
	if !requireFacetsEmployee(w, r) {
		return
	}
	id := chi.URLParam(r, "id")
	if !isValidJobID(id) {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	jobDir := filepath.Clean(filepath.Join(h.jobsDir, id))
	if !strings.HasPrefix(jobDir, filepath.Clean(h.jobsDir)+string(filepath.Separator)) {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	logPath := filepath.Join(jobDir, "agy.log")
	lines := tailLines(logPath, 200, 2000)

	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(map[string]any{"job_id": id, "lines": lines})
}

// tailLines returns up to `want` trailing lines of path (capped at `max`),
// or an empty slice if the file doesn't exist — never an error, since a job
// that hasn't logged anything yet (or ever) is a normal, expected state here,
// not a failure worth surfacing as one.
func tailLines(path string, want, max int) []string {
	if want > max {
		want = max
	}
	f, err := os.Open(path)
	if err != nil {
		return []string{}
	}
	defer f.Close()

	// Read the whole file and keep only the last `want` lines. Job logs are
	// bounded (one orchestrator run each), so this is simple and fine —
	// no need for a proper ring-buffer/seek-from-end reader here.
	var all []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		all = append(all, sc.Text())
	}
	if len(all) > want {
		all = all[len(all)-want:]
	}
	return all
}

// resolveJobDirs validates and resolves a list of job IDs to their on-disk
// directories. Shared by FacetsPortfolioSummary (JSON, POST body) and
// FacetsPortfolioSummaryXLSX (file download, GET query param) so the same
// validation (valid ID shape, path stays inside jobsDir, count cap) applies
// to both instead of being duplicated and potentially drifting apart.
func (h *Handler) resolveJobDirs(ids []string) ([]string, string) {
	if len(ids) == 0 {
		return nil, "job_ids must be a non-empty list"
	}
	if len(ids) > 100 {
		return nil, "too many job_ids (max 100)"
	}
	var jobDirs []string
	for _, id := range ids {
		if !isValidJobID(id) {
			return nil, "invalid job id: " + id
		}
		jobDirs = append(jobDirs, filepath.Clean(filepath.Join(h.jobsDir, id)))
	}
	return jobDirs, ""
}

// FacetsPortfolioSummary shells out to generate_portfolio_summary.py with
// the requested job directories and returns its JSON output as-is — the
// aggregation logic (AWS Total/Mapped/GCP OD/Diff per customer and per
// category) lives entirely in that script, not duplicated here in Go.
func (h *Handler) FacetsPortfolioSummary(w http.ResponseWriter, r *http.Request) {
	if !requireFacetsEmployee(w, r) {
		return
	}
	sess := auth.SessionFromCtx(r.Context())
	if wait, ok := checkPortfolioSummaryCooldown(sess.Email); !ok {
		w.Header().Set("Retry-After", fmt.Sprintf("%.0f", wait.Seconds()))
		http.Error(w, fmt.Sprintf(`{"error":"please wait %.0fs before generating another summary"}`, wait.Seconds()),
			http.StatusTooManyRequests)
		return
	}
	var body struct {
		JobIDs []string `json:"job_ids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, `{"error":"invalid json"}`, http.StatusBadRequest)
		return
	}
	jobDirs, errMsg := h.resolveJobDirs(body.JobIDs)
	if errMsg != "" {
		http.Error(w, `{"error":"`+jsonEscape(errMsg)+`"}`, http.StatusBadRequest)
		return
	}

	scriptPath := filepath.Join(skill.ResolveDir(), "scripts", "generate_portfolio_summary.py")
	args := append([]string{scriptPath}, jobDirs...)
	cmd := exec.Command(pythonBin(), args...)
	out, err := cmd.Output()
	if err != nil {
		msg := err.Error()
		if ee, ok := err.(*exec.ExitError); ok {
			msg = string(ee.Stderr)
		}
		http.Error(w, `{"error":"summary generation failed: `+jsonEscape(msg)+`"}`, http.StatusInternalServerError)
		return
	}

	w.Header().Set(contentTypeHeader, contentTypeJSON)
	w.Write(out)
}

// FacetsPortfolioSummaryHTML generates the portfolio summary as a
// self-contained HTML report — styled like the per-job report.html output,
// not a spreadsheet — and streams it back as a download. GET + query param
// (not POST + JSON body), same reasoning as the XLSX endpoint below: it lets
// the frontend trigger it via a plain <a href> browser download.
func (h *Handler) FacetsPortfolioSummaryHTML(w http.ResponseWriter, r *http.Request) {
	if !requireFacetsEmployee(w, r) {
		return
	}
	sess := auth.SessionFromCtx(r.Context())
	if wait, ok := checkPortfolioSummaryCooldown(sess.Email); !ok {
		w.Header().Set("Retry-After", fmt.Sprintf("%.0f", wait.Seconds()))
		http.Error(w, fmt.Sprintf(`{"error":"please wait %.0fs before generating another summary"}`, wait.Seconds()),
			http.StatusTooManyRequests)
		return
	}

	raw := r.URL.Query().Get("job_ids")
	var ids []string
	for _, id := range strings.Split(raw, ",") {
		id = strings.TrimSpace(id)
		if id != "" {
			ids = append(ids, id)
		}
	}
	jobDirs, errMsg := h.resolveJobDirs(ids)
	if errMsg != "" {
		http.Error(w, `{"error":"`+jsonEscape(errMsg)+`"}`, http.StatusBadRequest)
		return
	}

	tmpFile, err := os.CreateTemp("", "portfolio-summary-*.html")
	if err != nil {
		http.Error(w, `{"error":"could not create temp file"}`, http.StatusInternalServerError)
		return
	}
	tmpPath := tmpFile.Name()
	tmpFile.Close()
	defer os.Remove(tmpPath)

	scriptPath := filepath.Join(skill.ResolveDir(), "scripts", "generate_portfolio_summary.py")
	args := append([]string{scriptPath}, jobDirs...)
	args = append(args, "--html", tmpPath)
	cmd := exec.Command(pythonBin(), args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		http.Error(w, `{"error":"summary generation failed: `+jsonEscape(string(out))+`"}`, http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="portfolio-summary.html"`)
	http.ServeFile(w, r, tmpPath)
}

// FacetsPortfolioSummaryXLSX generates the same summary as an .xlsx file and
// streams it back as a download — GET + query param (not POST + JSON body)
// specifically so the frontend can trigger it via a plain <a href> browser
// download rather than a fetch-then-blob dance. job_ids is a comma-separated
// list. Shares the same per-session cooldown as the JSON endpoint (both
// invoke the same subprocess-per-job-file work), and a temp file that's
// always cleaned up after the response is written, success or failure.
func (h *Handler) FacetsPortfolioSummaryXLSX(w http.ResponseWriter, r *http.Request) {
	if !requireFacetsEmployee(w, r) {
		return
	}
	sess := auth.SessionFromCtx(r.Context())
	if wait, ok := checkPortfolioSummaryCooldown(sess.Email); !ok {
		w.Header().Set("Retry-After", fmt.Sprintf("%.0f", wait.Seconds()))
		http.Error(w, fmt.Sprintf(`{"error":"please wait %.0fs before generating another summary"}`, wait.Seconds()),
			http.StatusTooManyRequests)
		return
	}

	raw := r.URL.Query().Get("job_ids")
	var ids []string
	for _, id := range strings.Split(raw, ",") {
		id = strings.TrimSpace(id)
		if id != "" {
			ids = append(ids, id)
		}
	}
	jobDirs, errMsg := h.resolveJobDirs(ids)
	if errMsg != "" {
		http.Error(w, `{"error":"`+jsonEscape(errMsg)+`"}`, http.StatusBadRequest)
		return
	}

	tmpFile, err := os.CreateTemp("", "portfolio-summary-*.xlsx")
	if err != nil {
		http.Error(w, `{"error":"could not create temp file"}`, http.StatusInternalServerError)
		return
	}
	tmpPath := tmpFile.Name()
	tmpFile.Close()
	defer os.Remove(tmpPath)

	scriptPath := filepath.Join(skill.ResolveDir(), "scripts", "generate_portfolio_summary.py")
	args := append([]string{scriptPath}, jobDirs...)
	args = append(args, "--xlsx", tmpPath)
	cmd := exec.Command(pythonBin(), args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		http.Error(w, `{"error":"summary generation failed: `+jsonEscape(string(out))+`"}`, http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
	w.Header().Set("Content-Disposition", `attachment; filename="portfolio-summary.xlsx"`)
	http.ServeFile(w, r, tmpPath)
}

func jsonEscape(s string) string {
	b, _ := json.Marshal(s)
	// Marshal wraps in quotes; strip them since we're embedding inline above.
	if len(b) >= 2 {
		return string(b[1 : len(b)-1])
	}
	return s
}
