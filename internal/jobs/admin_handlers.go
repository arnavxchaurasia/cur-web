// internal/jobs/admin_handlers.go
package jobs

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
)

// ─────────────────────────────────────────────────────────────────────────
// Admin: paginated/filtered job listing, cancel, delete
// ─────────────────────────────────────────────────────────────────────────

const defaultAdminPageSize = 50

// AdminListAllPaged extends AdminListAll with limit/offset/from/to/status
// query params and a total count for pagination. Registered at the same
// GET /api/admin/jobs route (replaces the plain AdminListAll handler).
func (h *Handler) AdminListAllPaged(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := defaultAdminPageSize
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	offset := 0
	if v := q.Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}
	status := q.Get("status")
	var from, to *time.Time
	if v := q.Get("from"); v != "" {
		if t, err := time.Parse("2006-01-02", v); err == nil {
			from = &t
		}
	}
	if v := q.Get("to"); v != "" {
		if t, err := time.Parse("2006-01-02", v); err == nil {
			// Inclusive end-of-day.
			t = t.Add(24*time.Hour - time.Second)
			to = &t
		}
	}

	jobList, err := h.db.ListAllJobsFiltered(limit, offset, status, from, to)
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	if jobList == nil {
		jobList = []*db.Job{}
	}
	total, err := h.db.CountAllJobs(status, from, to)
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(map[string]any{
		"jobs":   jobList,
		"total":  total,
		"limit":  limit,
		"offset": offset,
	})
}

// AdminCancelJob kills the job's process (if running) and marks it cancelled.
func (h *Handler) AdminCancelJob(w http.ResponseWriter, r *http.Request) {
	sess := auth.SessionFromCtx(r.Context())
	id := chi.URLParam(r, "id")
	if !isValidJobID(id) {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	job, err := h.db.GetJob(id)
	if err != nil {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	if job.AgentPID > 0 && pidAlive(job.AgentPID) {
		killGroup(job.AgentPID)
	}
	if err := h.db.UpdateJobCancelled(id); err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	_ = h.db.LogAdminAction(sess.Email, "cancel_job", id, "")
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(map[string]string{"id": id, "status": "cancelled"})
}

// AdminDeleteJob removes a terminal-status job's row and on-disk directory.
func (h *Handler) AdminDeleteJob(w http.ResponseWriter, r *http.Request) {
	sess := auth.SessionFromCtx(r.Context())
	id := chi.URLParam(r, "id")
	jobDir, ok := safeJobDir(h.jobsDir, id)
	if !ok {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	job, err := h.db.GetJob(id)
	if err != nil {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	switch job.Status {
	case "done", "failed", "cancelled":
		// terminal — ok to delete
	default:
		http.Error(w, `{"error":"can only delete a job in a terminal state"}`, http.StatusConflict)
		return
	}
	if err := os.RemoveAll(jobDir); err != nil {
		http.Error(w, `{"error":"failed to remove job directory"}`, http.StatusInternalServerError)
		return
	}
	if err := h.db.DeleteJob(id); err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	_ = h.db.LogAdminAction(sess.Email, "delete_job", id, "")
	w.WriteHeader(http.StatusNoContent)
}

// ─────────────────────────────────────────────────────────────────────────
// Admin: users / RBAC
// ─────────────────────────────────────────────────────────────────────────

func (h *Handler) AdminListUsers(w http.ResponseWriter, r *http.Request) {
	users, err := h.db.ListUsers()
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	if users == nil {
		users = []*db.User{}
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(users)
}

var validRoles = map[string]bool{"viewer": true, "admin": true, "facets": true}

func (h *Handler) AdminSetUserRole(w http.ResponseWriter, r *http.Request) {
	sess := auth.SessionFromCtx(r.Context())
	email := chi.URLParam(r, "email")
	var body struct {
		Role string `json:"role"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || !validRoles[body.Role] {
		http.Error(w, `{"error":"role must be one of viewer, admin, facets"}`, http.StatusBadRequest)
		return
	}
	if err := h.db.SetUserRole(email, body.Role); err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	_ = h.db.LogAdminAction(sess.Email, "set_user_role", email, body.Role)
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(map[string]string{"email": email, "role": body.Role})
}

// ─────────────────────────────────────────────────────────────────────────
// Admin: catalog status / forced refresh
// ─────────────────────────────────────────────────────────────────────────

func (h *Handler) AdminCatalogStatus(w http.ResponseWriter, r *http.Request) {
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(GetCatalogStatus())
}

func (h *Handler) AdminCatalogRefresh(w http.ResponseWriter, r *http.Request) {
	sess := auth.SessionFromCtx(r.Context())
	TriggerCatalogRefresh()
	_ = h.db.LogAdminAction(sess.Email, "catalog_refresh", "", "")
	w.WriteHeader(http.StatusAccepted)
	json.NewEncoder(w).Encode(map[string]string{"status": "refresh started"})
}

// ─────────────────────────────────────────────────────────────────────────
// Admin: audit log / analytics
// ─────────────────────────────────────────────────────────────────────────

func (h *Handler) AdminAuditLog(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := 100
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	offset := 0
	if v := q.Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}
	entries, err := h.db.ListAuditLog(limit, offset)
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	if entries == nil {
		entries = []*db.AuditEntry{}
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(entries)
}

func (h *Handler) AdminAnalytics(w http.ResponseWriter, r *http.Request) {
	stats, err := h.db.GetUsageStats()
	if err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(stats)
}

// ─────────────────────────────────────────────────────────────────────────
// Shareable report links
// ─────────────────────────────────────────────────────────────────────────

// ShareJob creates a report_shares row for job {id} (owner/admin only) and
// returns the public URL for it. Shares expire after 30 days.
func (h *Handler) ShareJob(w http.ResponseWriter, r *http.Request) {
	sess := auth.SessionFromCtx(r.Context())
	id := chi.URLParam(r, "id")
	if !isValidJobID(id) {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	job, err := h.db.GetJob(id)
	if err != nil {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	if !h.canAccess(sess, job) {
		http.Error(w, `{"error":"forbidden"}`, http.StatusForbidden)
		return
	}
	if job.Status != "done" {
		http.Error(w, `{"error":"can only share a completed job"}`, http.StatusConflict)
		return
	}
	token := uuid.New().String()
	expiresAt := time.Now().Add(30 * 24 * time.Hour)
	if err := h.db.CreateReportShare(token, id, sess.Email, expiresAt); err != nil {
		http.Error(w, `{"error":"db error"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(map[string]string{
		"token": token,
		"url":   "/reports/" + token,
	})
}

// GetSharedReport is the PUBLIC (no-auth) read-only summary lookup for a
// share token. Reuses the same run_results summary-file resolution as
// Handler.Summary.
func (h *Handler) GetSharedReport(w http.ResponseWriter, r *http.Request) {
	token := chi.URLParam(r, "token")
	share, err := h.db.GetReportShare(token)
	if err != nil || share == nil {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	if share.ExpiresAt != nil && time.Now().After(*share.ExpiresAt) {
		http.Error(w, `{"error":"this share link has expired"}`, http.StatusGone)
		return
	}
	job, err := h.db.GetJob(share.JobID)
	if err != nil {
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}
	jobDir := filepath.Clean(filepath.Join(h.jobsDir, share.JobID))
	dbPath := filepath.Join(jobDir, projectionAuditDir, projectionDuckDB)
	runs, err := QueryRunResults(dbPath)
	if err != nil {
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}
	var pick *RunResult
	if len(runs) > 0 {
		pick = &runs[0]
	}
	resp := map[string]any{
		"prospect":   job.Prospect,
		"status":     job.Status,
		"created_at": job.CreatedAt,
	}
	if pick != nil && pick.SummaryMD != nil && *pick.SummaryMD != "" {
		p := filepath.Join(jobDir, *pick.SummaryMD)
		if data, err := os.ReadFile(p); err == nil {
			resp["summary_md"] = string(data)
		}
	}
	w.Header().Set(contentTypeHeader, contentTypeJSON)
	json.NewEncoder(w).Encode(resp)
}
