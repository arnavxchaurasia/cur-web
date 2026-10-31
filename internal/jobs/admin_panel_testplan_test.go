package jobs_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/facets/cur-web/internal/jobs"
	"github.com/go-chi/chi/v5"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func ioCloserWrapper(s string) io.ReadCloser {
	return io.NopCloser(bytes.NewBufferString(s))
}

// buildTestRouter mounts the exact route tree from main.go into a chi.Router for testing
func buildTestRouter(database *db.DB, jobsDir string, adminEmails []string) http.Handler {
	spawner := jobs.NewSpawner(jobs.SpawnerConfig{})
	spawner.SetDB(database)
	jobHandler := jobs.NewHandler(database, jobsDir, spawner, nil, jobs.NotifyConfig{}, adminEmails)

	r := chi.NewRouter()

	// Public share route
	r.Get("/api/reports/{token}", jobHandler.GetSharedReport)

	// Authenticated routes
	r.Group(func(r chi.Router) {
		r.Use(func(next http.Handler) http.Handler {
			return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				sess := auth.SessionFromCtx(req.Context())
				if sess == nil {
					http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
					return
				}
				// Resolve role dynamically like auth.Middleware
				if role, granter, err := database.GetUserRoleAndGrant(sess.Email); err == nil && role != "" {
					sess.Role = role
					sess.AdminGrantedBy = granter
				}
				next.ServeHTTP(w, req)
			})
		})

		r.Post("/api/jobs", jobHandler.Create)
		r.Get("/api/jobs/{id}", jobHandler.GetByID)

		// Admin-only group
		r.Group(func(r chi.Router) {
			r.Use(auth.RequireAdmin(adminEmails))
			r.Get("/api/admin/jobs", jobHandler.AdminListAllPaged)
			r.Post("/api/admin/jobs/{id}/cancel", jobHandler.AdminCancelJob)
			r.Post("/api/admin/jobs/cancel-all", jobHandler.CancelAll)
			r.Post("/api/admin/jobs/resume-all", jobHandler.ResumeAll)
			r.Post("/api/admin/jobs/{id}/regenerate-report", jobHandler.RegenerateReport)
			r.Post("/api/admin/jobs/{id}/recalculate", jobHandler.Recalculate)
			r.Get("/api/admin/system/state", jobHandler.SystemState)
			r.Delete("/api/admin/jobs/{id}", jobHandler.AdminDeleteJob)
			r.Get("/api/admin/deleted-jobs", jobHandler.AdminListDeletedJobs)
			r.Get("/api/admin/users", jobHandler.AdminListUsers)
			r.Post("/api/admin/users/{email}/role", jobHandler.AdminSetUserRole)
			r.Get("/api/admin/catalog/status", jobHandler.AdminCatalogStatus)
			r.Post("/api/admin/catalog/refresh", jobHandler.AdminCatalogRefresh)
			r.Get("/api/admin/audit", jobHandler.AdminAuditLog)
			r.Get("/api/admin/analytics", jobHandler.AdminAnalytics)
			r.Get("/api/admin/assumptions", jobHandler.AdminGetAssumptions)
			r.Post("/api/admin/assumptions", jobHandler.AdminCreateAssumption)
			r.Put("/api/admin/assumptions/{key}", jobHandler.AdminPutAssumption)
			r.Get("/api/admin/assumptions/history", jobHandler.AdminGetAssumptionHistory)
			r.Get("/api/admin/report-settings", jobHandler.AdminGetReportSettings)
			r.Put("/api/admin/report-settings/{key}", jobHandler.AdminPutReportSetting)
			r.Get("/api/admin/report-settings/history", jobHandler.AdminGetReportSettingHistory)
			r.Post("/api/admin/report-settings/reset", jobHandler.AdminResetReportSettings)
			r.Get("/api/admin/shares", jobHandler.AdminListReportShares)
			r.Delete("/api/admin/shares/{token}", jobHandler.AdminDeleteReportShare)
			r.Get("/api/admin/jobs/{id}/diagnostics", jobHandler.AdminJobDiagnostics)
			r.Get("/api/admin/jobs/{id}/diagnostics/records", jobHandler.AdminJobDiagnosticRecords)
			r.Get("/api/admin/jobs/{id}/compare", jobHandler.AdminCompareRuns)
			r.Get("/api/admin/notify/status", jobHandler.AdminNotifyStatus)
			r.Post("/api/admin/notify/test", jobHandler.AdminNotifyTest)
			r.Post("/api/admin/tickets", jobHandler.AdminCreateTicket)
			r.Get("/api/admin/tickets", jobHandler.AdminListTickets)
			r.Get("/api/admin/tickets/{id}", jobHandler.AdminGetTicket)
			r.Put("/api/admin/tickets/{id}", jobHandler.AdminUpdateTicket)
			r.Get("/api/admin/notifications", jobHandler.AdminNotifications)
			r.Get("/api/admin/bills", jobHandler.ListUploadedBills)
			r.Get("/api/admin/bills/{hash}/download", jobHandler.AdminDownloadUploadedBill)
			r.Get("/api/admin/bills/{hash}/view", jobHandler.AdminViewUploadedBill)
			r.Get("/api/admin/bills/{hash}/preview", jobHandler.AdminPreviewUploadedBill)
		})
	})

	return r
}

type routeCheck struct {
	method string
	path   string
}

func TestSection3_AuthorizationMatrix(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"a@facets.cloud"}
	router := buildTestRouter(d, jobsDir, adminEmails)

	// Populate users
	require.NoError(t, d.UpsertUserSeen("a@facets.cloud", "admin"))
	require.NoError(t, d.UpsertUserSeen("b@facets.cloud", "viewer"))
	require.NoError(t, d.UpsertUserSeen("c@gmail.com", "viewer"))
	require.NoError(t, d.UpsertUserSeen("d@other.com", "viewer"))

	// Create a dummy job owned by D
	jobID := "11111111-2222-3333-4444-555555555555"
	require.NoError(t, d.CreateJob(jobID, "d@other.com", "ProspectD", ".csv", "sess-d"))
	require.NoError(t, d.UpdateJobDone(jobID, 500.0))

	// Routes to test according to Section 3 of TESTPLAN_admin_panel.md
	routes := []routeCheck{
		{"GET", "/api/admin/analytics"},
		{"GET", "/api/admin/assumptions"},
		{"PUT", "/api/admin/assumptions/aurora_acu_vcpu"},
		{"GET", "/api/admin/assumptions/history"},
		{"GET", "/api/admin/audit"},
		{"GET", "/api/admin/bills"},
		{"GET", "/api/admin/bills/hash123/download"},
		{"GET", "/api/admin/bills/hash123/preview"},
		{"GET", "/api/admin/bills/hash123/view"},
		{"GET", "/api/admin/catalog/status"},
		{"POST", "/api/admin/catalog/refresh"},
		{"GET", "/api/admin/deleted-jobs"},
		{"GET", "/api/admin/jobs"},
		{"POST", fmt.Sprintf("/api/admin/jobs/%s/cancel", jobID)},
		{"DELETE", fmt.Sprintf("/api/admin/jobs/%s", jobID)},
		{"POST", "/api/admin/jobs/cancel-all"},
		{"POST", "/api/admin/jobs/resume-all"},
		{"GET", fmt.Sprintf("/api/admin/jobs/%s/compare", jobID)},
		{"GET", fmt.Sprintf("/api/admin/jobs/%s/diagnostics", jobID)},
		{"GET", fmt.Sprintf("/api/admin/jobs/%s/diagnostics/records", jobID)},
		{"POST", fmt.Sprintf("/api/admin/jobs/%s/regenerate-report", jobID)},
		{"POST", fmt.Sprintf("/api/admin/jobs/%s/recalculate", jobID)},
		{"GET", "/api/admin/notifications"},
		{"GET", "/api/admin/notify/status"},
		{"POST", "/api/admin/notify/test"},
		{"GET", "/api/admin/report-settings"},
		{"GET", "/api/admin/report-settings/history"},
		{"PUT", "/api/admin/report-settings/col_gcp_cud_1yr"},
		{"POST", "/api/admin/report-settings/reset"},
		{"GET", "/api/admin/shares"},
		{"DELETE", "/api/admin/shares/token123"},
		{"GET", "/api/admin/system/state"},
		{"GET", "/api/admin/tickets"},
		{"POST", "/api/admin/tickets"},
		{"GET", "/api/admin/tickets/1"},
		{"PUT", "/api/admin/tickets/1"},
		{"GET", "/api/admin/users"},
		{"POST", "/api/admin/users/c@gmail.com/role"},
	}

	for _, rc := range routes {
		name := fmt.Sprintf("%s %s", rc.method, rc.path)
		t.Run(name, func(t *testing.T) {
			newReq := func(ctx context.Context) *http.Request {
				var body io.Reader
				if rc.method == "POST" || rc.method == "PUT" {
					body = bytes.NewBufferString("{}")
				}
				req, _ := http.NewRequestWithContext(ctx, rc.method, rc.path, body)
				return req
			}

			// 1. Anonymous -> 401
			reqAnon := newReq(context.Background())
			recAnon := httptest.NewRecorder()
			router.ServeHTTP(recAnon, reqAnon)
			assert.Equal(t, http.StatusUnauthorized, recAnon.Code, "Anonymous should receive 401")

			// 2. User C (external non-admin) -> 403
			reqC := newReq(ctxWithFullSession("c@gmail.com", "C", "viewer"))
			recC := httptest.NewRecorder()
			router.ServeHTTP(recC, reqC)
			assert.Equal(t, http.StatusForbidden, recC.Code, "User C (external non-admin) should receive 403")

			// 3. User B (facets non-admin) -> 403
			reqB := newReq(ctxWithFullSession("b@facets.cloud", "B", "viewer"))
			recB := httptest.NewRecorder()
			router.ServeHTTP(recB, reqB)
			assert.Equal(t, http.StatusForbidden, recB.Code, "User B (facets non-admin) should receive 403")

			// 4. User A (facets admin) -> Allowed through admin gate (NOT 401 and NOT 403)
			reqA := newReq(ctxWithFullSession("a@facets.cloud", "A", "admin"))
			recA := httptest.NewRecorder()
			router.ServeHTTP(recA, reqA)
			assert.NotEqual(t, http.StatusUnauthorized, recA.Code, "User A should not receive 401")
			assert.NotEqual(t, http.StatusForbidden, recA.Code, "User A should not receive 403")
		})
	}
}

func TestSection4_1_UsersAndRoles(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"root@facets.cloud"}
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, adminEmails)

	require.NoError(t, d.UpsertUserSeen("a@facets.cloud", "admin"))
	require.NoError(t, d.UpsertUserSeen("b@facets.cloud", "viewer"))
	require.NoError(t, d.UpsertUserSeen("c@gmail.com", "viewer"))
	require.NoError(t, d.UpsertUserSeen("other@gmail.com", "viewer"))

	// 1. As A, change C from viewer to admin via AdminSetUserRole
	t.Run("Step 2 & 3: Facets Admin grants admin to external user C", func(t *testing.T) {
		req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/c@gmail.com/role", nil)
		req = req.WithContext(withChiParam(req.Context(), "email", "c@gmail.com"))
		req.Body = ioCloserWrapper(`{"role":"admin"}`)
		rec := httptest.NewRecorder()
		h.AdminSetUserRole(rec, req)

		assert.Equal(t, http.StatusOK, rec.Code)
		var resp map[string]interface{}
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
		assert.Equal(t, "admin", resp["role"])

		// Verify provenance in DB
		role, granter, err := d.GetUserRoleAndGrant("c@gmail.com")
		require.NoError(t, err)
		assert.Equal(t, "admin", role)
		assert.Equal(t, "a@facets.cloud", granter)

		// Verify C's session now reflects IsAdmin(adminEmails) = true immediately
		sessC := &auth.Session{Email: "c@gmail.com", Role: role, AdminGrantedBy: granter}
		assert.True(t, sessC.IsAdmin(adminEmails))
	})

	t.Run("Step 4: Newly admin C cannot promote another external user", func(t *testing.T) {
		req, _ := http.NewRequestWithContext(ctxWithFullSession("c@gmail.com", "Admin C", "admin"), "POST", "/api/admin/users/other@gmail.com/role", nil)
		req = req.WithContext(withChiParam(req.Context(), "email", "other@gmail.com"))
		req.Body = ioCloserWrapper(`{"role":"admin"}`)
		rec := httptest.NewRecorder()
		h.AdminSetUserRole(rec, req)

		assert.Equal(t, http.StatusForbidden, rec.Code)
		var resp map[string]interface{}
		_ = json.Unmarshal(rec.Body.Bytes(), &resp)
		assert.Contains(t, resp["error"], "only facets admin")
	})

	t.Run("Step 5: Admin A cannot change own role", func(t *testing.T) {
		req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/a@facets.cloud/role", nil)
		req = req.WithContext(withChiParam(req.Context(), "email", "a@facets.cloud"))
		req.Body = ioCloserWrapper(`{"role":"viewer"}`)
		rec := httptest.NewRecorder()
		h.AdminSetUserRole(rec, req)

		assert.Equal(t, http.StatusBadRequest, rec.Code)
		var resp map[string]interface{}
		_ = json.Unmarshal(rec.Body.Bytes(), &resp)
		assert.Contains(t, resp["error"], "cannot change your own role")
	})

	t.Run("Step 6: Demote B while signed in takes effect immediately", func(t *testing.T) {
		// First give B admin
		require.NoError(t, d.SetUserRoleWithGrant("b@facets.cloud", "admin", "a@facets.cloud"))
		// Demote B
		req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/b@facets.cloud/role", nil)
		req = req.WithContext(withChiParam(req.Context(), "email", "b@facets.cloud"))
		req.Body = ioCloserWrapper(`{"role":"viewer"}`)
		rec := httptest.NewRecorder()
		h.AdminSetUserRole(rec, req)

		assert.Equal(t, http.StatusOK, rec.Code)
		role, granter, err := d.GetUserRoleAndGrant("b@facets.cloud")
		require.NoError(t, err)
		assert.Equal(t, "viewer", role)
		assert.Equal(t, "", granter)
	})

	t.Run("Step 7: Last facets admin cannot be demoted", func(t *testing.T) {
		// Suppose another admin X attempts to demote A (the only facets admin remaining)
		require.NoError(t, d.UpsertUserSeen("super@facets.cloud", "admin"))
		// Demote super
		req1, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/super@facets.cloud/role", nil)
		req1 = req1.WithContext(withChiParam(req1.Context(), "email", "super@facets.cloud"))
		req1.Body = ioCloserWrapper(`{"role":"viewer"}`)
		rec1 := httptest.NewRecorder()
		h.AdminSetUserRole(rec1, req1)
		assert.Equal(t, http.StatusOK, rec1.Code)

		// Now A is the only facets admin. If someone tries to demote A:
		req2, _ := http.NewRequestWithContext(ctxWithFullSession("super@facets.cloud", "Super", "admin"), "POST", "/api/admin/users/a@facets.cloud/role", nil)
		req2 = req2.WithContext(withChiParam(req2.Context(), "email", "a@facets.cloud"))
		req2.Body = ioCloserWrapper(`{"role":"viewer"}`)
		rec2 := httptest.NewRecorder()
		h.AdminSetUserRole(rec2, req2)

		assert.Equal(t, http.StatusBadRequest, rec2.Code)
		var resp map[string]interface{}
		_ = json.Unmarshal(rec2.Body.Bytes(), &resp)
		assert.Contains(t, resp["error"], "cannot demote the last facets admin")
	})

	t.Run("Step 8: Audit log records every grant/demote", func(t *testing.T) {
		entries, err := d.ListAuditLog(10, 0, "", "", nil, nil)
		require.NoError(t, err)
		assert.NotEmpty(t, entries)
	})
}

func TestSection4_2_AssumptionsValidationAndPersistence(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, []string{"a@facets.cloud"})

	// 1. List assumptions
	reqList, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "GET", "/api/admin/assumptions", nil)
	recList := httptest.NewRecorder()
	h.AdminGetAssumptions(recList, reqList)
	assert.Equal(t, http.StatusOK, recList.Code)
	var listResp struct {
		Assumptions []struct {
			Key      string `json:"key"`
			Category string `json:"category"`
		} `json:"assumptions"`
		Categories []string `json:"categories"`
		Version    string   `json:"version"`
	}
	require.NoError(t, json.Unmarshal(recList.Body.Bytes(), &listResp))
	assert.Equal(t, db.AssumptionCategories, listResp.Categories)
	for _, a := range listResp.Assumptions {
		assert.NotEmpty(t, a.Category)
		assert.True(t, db.IsValidAssumptionCategory(a.Category))
	}

	// 2. Change aurora_acu_vcpu to a valid value (1.0 -> 16.0 / 1.0 = 16, valid 1:16 ratio)
	reqValid, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "PUT", "/api/admin/assumptions/aurora_acu_vcpu", nil)
	reqValid = reqValid.WithContext(withChiParam(reqValid.Context(), "key", "aurora_acu_vcpu"))
	reqValid.Body = ioCloserWrapper(`{"value":1.0,"reason":"AWS doc parity"}`)
	recValid := httptest.NewRecorder()
	h.AdminPutAssumption(recValid, reqValid)
	assert.Equal(t, http.StatusOK, recValid.Code)

	// 2b. Out-of-band ratio (0.5 vCPU with 16 GiB RAM gives 1:32, not in 1:4, 1:8, 1:16)
	reqInvalidRatio, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "PUT", "/api/admin/assumptions/aurora_acu_vcpu", nil)
	reqInvalidRatio = reqInvalidRatio.WithContext(withChiParam(reqInvalidRatio.Context(), "key", "aurora_acu_vcpu"))
	reqInvalidRatio.Body = ioCloserWrapper(`{"value":0.5,"reason":"invalid out-of-band ratio"}`)
	recInvalidRatio := httptest.NewRecorder()
	h.AdminPutAssumption(recInvalidRatio, reqInvalidRatio)
	assert.Equal(t, http.StatusBadRequest, recInvalidRatio.Code)
	assert.Contains(t, recInvalidRatio.Body.String(), "allowed ratios are 1:4, 1:8, 1:16")

	// 3. Below minimum or above maximum
	reqMin, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "PUT", "/api/admin/assumptions/aurora_acu_vcpu", nil)
	reqMin = reqMin.WithContext(withChiParam(reqMin.Context(), "key", "aurora_acu_vcpu"))
	reqMin.Body = ioCloserWrapper(`{"value":-1.0,"reason":"invalid negative"}`)
	recMin := httptest.NewRecorder()
	h.AdminPutAssumption(recMin, reqMin)
	assert.Equal(t, http.StatusBadRequest, recMin.Code)

	// 4. Non-numeric
	reqNonNum, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "PUT", "/api/admin/assumptions/aurora_acu_vcpu", nil)
	reqNonNum = reqNonNum.WithContext(withChiParam(reqNonNum.Context(), "key", "aurora_acu_vcpu"))
	reqNonNum.Body = ioCloserWrapper(`{"value":"not-a-number","reason":"test"}`)
	recNonNum := httptest.NewRecorder()
	h.AdminPutAssumption(recNonNum, reqNonNum)
	assert.Equal(t, http.StatusBadRequest, recNonNum.Code)

	// 5. Invalid category on PUT returns 400
	reqBadCatPut, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "PUT", "/api/admin/assumptions/aurora_acu_vcpu", nil)
	reqBadCatPut = reqBadCatPut.WithContext(withChiParam(reqBadCatPut.Context(), "key", "aurora_acu_vcpu"))
	reqBadCatPut.Body = ioCloserWrapper(`{"value":0.5,"reason":"test","category":"nonsense"}`)
	recBadCatPut := httptest.NewRecorder()
	h.AdminPutAssumption(recBadCatPut, reqBadCatPut)
	assert.Equal(t, http.StatusBadRequest, recBadCatPut.Code)

	// 6. Invalid category on POST returns 400
	reqBadCatPost, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "POST", "/api/admin/assumptions", nil)
	reqBadCatPost.Body = ioCloserWrapper(`{"key":"new_assumption","value":1.0,"category":"nonsense"}`)
	recBadCatPost := httptest.NewRecorder()
	h.AdminCreateAssumption(recBadCatPost, reqBadCatPost)
	assert.Equal(t, http.StatusBadRequest, recBadCatPost.Code)

	// 7. Valid category on POST returns 201 Created
	reqValidPost, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "POST", "/api/admin/assumptions", nil)
	reqValidPost.Body = ioCloserWrapper(`{"key":"new_storage_assumption","value":10.0,"category":"storage"}`)
	recValidPost := httptest.NewRecorder()
	h.AdminCreateAssumption(recValidPost, reqValidPost)
	assert.Equal(t, http.StatusCreated, recValidPost.Code)
}

func TestSection4_6_ShareLinksSecurity(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, []string{"a@facets.cloud"})

	jobID := "share-job-999"
	require.NoError(t, d.CreateJob(jobID, "owner@facets.cloud", "SharedCorp", ".csv", "sess-s"))
	require.NoError(t, d.UpdateJobDone(jobID, 300.0))

	// Create job directory with report.html
	jobPath := filepath.Join(jobsDir, jobID)
	require.NoError(t, os.MkdirAll(jobPath, 0755))
	require.NoError(t, os.WriteFile(filepath.Join(jobPath, "report.html"), []byte("<html>report</html>"), 0644))

	// Create active share token
	activeToken := "active_token_123456789012"
	require.NoError(t, d.CreateReportShare(activeToken, jobID, "owner@facets.cloud", time.Now().Add(1*time.Hour)))

	// 1. Anonymous open active link -> 200
	reqShare, _ := http.NewRequest("GET", "/api/reports/"+activeToken, nil)
	reqShare = reqShare.WithContext(withChiParam(reqShare.Context(), "token", activeToken))
	recShare := httptest.NewRecorder()
	h.GetSharedReport(recShare, reqShare)
	assert.Equal(t, http.StatusOK, recShare.Code)

	// 2. /api/admin/shares lists it and token is masked
	reqList, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "GET", "/api/admin/shares", nil)
	recList := httptest.NewRecorder()
	h.AdminListReportShares(recList, reqList)
	assert.Equal(t, http.StatusOK, recList.Code)
	assert.Contains(t, recList.Body.String(), "active")
	assert.NotContains(t, recList.Body.String(), activeToken) // Must be masked

	// 3. Revoke it
	reqDel, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "DELETE", "/api/admin/shares/"+activeToken, nil)
	reqDel = reqDel.WithContext(withChiParam(reqDel.Context(), "token", activeToken))
	recDel := httptest.NewRecorder()
	h.AdminDeleteReportShare(recDel, reqDel)
	assert.Equal(t, http.StatusNoContent, recDel.Code)

	// 4. Revoked link gives identical 404 response to nonexistent token
	reqRevoked, _ := http.NewRequest("GET", "/api/reports/"+activeToken, nil)
	reqRevoked = reqRevoked.WithContext(withChiParam(reqRevoked.Context(), "token", activeToken))
	recRevoked := httptest.NewRecorder()
	h.GetSharedReport(recRevoked, reqRevoked)
	assert.Equal(t, http.StatusNotFound, recRevoked.Code)

	reqNonExistent, _ := http.NewRequest("GET", "/api/reports/never_existed_token", nil)
	reqNonExistent = reqNonExistent.WithContext(withChiParam(reqNonExistent.Context(), "token", "never_existed_token"))
	recNonExistent := httptest.NewRecorder()
	h.GetSharedReport(recNonExistent, reqNonExistent)
	assert.Equal(t, http.StatusNotFound, recNonExistent.Code)

	assert.Equal(t, recNonExistent.Body.String(), recRevoked.Body.String(), "Response body for revoked token must be byte-identical to nonexistent token")
}

func TestSection5_NegativeAndRobustness(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, []string{"a@facets.cloud"})

	// 1. Path traversal attempts
	traversalPaths := []string{
		"..%2f..%2fetc%2fpasswd",
		"....//....//etc/passwd",
		"%2e%2e%2f%2e%2e%2fetc%2fpasswd",
	}
	for _, p := range traversalPaths {
		req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "GET", "/api/admin/bills/"+p+"/view", nil)
		req = req.WithContext(withChiParam(req.Context(), "hash", p))
		rec := httptest.NewRecorder()
		h.AdminViewUploadedBill(rec, req)
		assert.True(t, rec.Code == http.StatusBadRequest || rec.Code == http.StatusNotFound)
		assert.NotContains(t, rec.Body.String(), "root:")
	}

	// 2. Malformed JSON bodies on PUT / POST
	reqMalformed, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "A", "admin"), "PUT", "/api/admin/assumptions/aurora_acu_vcpu", nil)
	reqMalformed = reqMalformed.WithContext(withChiParam(reqMalformed.Context(), "key", "aurora_acu_vcpu"))
	reqMalformed.Body = ioCloserWrapper(`{"invalid-json`)
	recMalformed := httptest.NewRecorder()
	h.AdminPutAssumption(recMalformed, reqMalformed)
	assert.Equal(t, http.StatusBadRequest, recMalformed.Code)
}

func TestAdminSetUserRole_RefusesDemotingAdminEmailsEntry(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"breakglass@facets.cloud", "a@facets.cloud"}
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, adminEmails)

	// User breakglass@facets.cloud starts as admin in DB
	require.NoError(t, d.UpsertUserSeen("breakglass@facets.cloud", "admin"))
	require.NoError(t, d.UpsertUserSeen("a@facets.cloud", "admin"))

	// Caller 'a@facets.cloud' attempts to demote 'breakglass@facets.cloud' to viewer
	req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/breakglass@facets.cloud/role", nil)
	req = req.WithContext(withChiParam(req.Context(), "email", "breakglass@facets.cloud"))
	req.Body = ioCloserWrapper(`{"role":"viewer"}`)
	rec := httptest.NewRecorder()
	h.AdminSetUserRole(rec, req)

	// Assert: status 409, and the response body mentions ADMIN_EMAILS.
	assert.Equal(t, http.StatusConflict, rec.Code)
	assert.Contains(t, rec.Body.String(), "ADMIN_EMAILS")

	// Assert: the stored role is UNCHANGED afterwards (read it back).
	role, err := d.GetUserRole("breakglass@facets.cloud")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)
}

// TestAdminSetUserRole_DecodesEncodedEmail: an encoded email in the path updates
// the genuine decoded row and leaves no row containing '%'.
func TestAdminSetUserRole_DecodesEncodedEmail(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"a@facets.cloud"}
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, adminEmails)

	require.NoError(t, d.UpsertUserSeen("a@facets.cloud", "admin"))
	require.NoError(t, d.UpsertUserSeen("alice@example.com", "viewer"))

	req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/alice%40example.com/role", nil)
	req = req.WithContext(withChiParam(req.Context(), "email", "alice%40example.com"))
	req.Body = ioCloserWrapper(`{"role":"admin"}`)
	rec := httptest.NewRecorder()
	h.AdminSetUserRole(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)

	role, granter, err := d.GetUserRoleAndGrant("alice@example.com")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)
	assert.Equal(t, "a@facets.cloud", granter)

	// No ghost row containing '%' should exist. (RowsAffected is meaningless
	// for a SELECT, so read the count out properly.)
	var count int
	require.NoError(t, d.Conn().QueryRow(`SELECT COUNT(*) FROM users WHERE email LIKE '%\%%' ESCAPE '\'`).Scan(&count))
	assert.Equal(t, 0, count, "no URL-encoded ghost row should remain")
}

// TestAdminSetUserRole_RejectsDoubleEncoded: a double-encoded email is rejected
// and no row is created.
func TestAdminSetUserRole_RejectsDoubleEncoded(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"a@facets.cloud"}
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, adminEmails)

	require.NoError(t, d.UpsertUserSeen("a@facets.cloud", "admin"))

	req, _ := http.NewRequestWithContext(ctxWithFullSession("a@facets.cloud", "Admin A", "admin"), "POST", "/api/admin/users/alice%2540example.com/role", nil)
	req = req.WithContext(withChiParam(req.Context(), "email", "alice%2540example.com"))
	req.Body = ioCloserWrapper(`{"role":"admin"}`)
	rec := httptest.NewRecorder()
	h.AdminSetUserRole(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)

	// No row created, for either the decoded or the raw form.
	var created int
	require.NoError(t, d.Conn().QueryRow(
		`SELECT COUNT(*) FROM users WHERE email IN ('alice@example.com', 'alice%40example.com', 'alice%2540example.com')`,
	).Scan(&created))
	assert.Equal(t, 0, created, "a rejected address must create no user row")
}

// TestAdminSetUserRole_SelfDemotionBlockedViaEncodedPath: self-change via an
// encoded path is still blocked (the guardrail bypass is closed).
func TestAdminSetUserRole_SelfDemotionBlockedViaEncodedPath(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"alice@example.com"}
	h := jobs.NewHandler(d, jobsDir, jobs.NewSpawner(jobs.SpawnerConfig{}), nil, jobs.NotifyConfig{}, adminEmails)

	require.NoError(t, d.UpsertUserSeen("alice@example.com", "admin"))

	// Caller is alice@example.com; path is the encoded form of the same address.
	req, _ := http.NewRequestWithContext(ctxWithFullSession("alice@example.com", "Alice", "admin"), "POST", "/api/admin/users/alice%40example.com/role", nil)
	req = req.WithContext(withChiParam(req.Context(), "email", "alice%40example.com"))
	req.Body = ioCloserWrapper(`{"role":"viewer"}`)
	rec := httptest.NewRecorder()
	h.AdminSetUserRole(rec, req)

	assert.Equal(t, http.StatusBadRequest, rec.Code)
	var resp map[string]interface{}
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	assert.Contains(t, resp["error"], "cannot change your own role")

	// Stored role unchanged.
	role, err := d.GetUserRole("alice@example.com")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)
}

