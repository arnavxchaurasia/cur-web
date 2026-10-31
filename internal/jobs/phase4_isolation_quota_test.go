package jobs_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/facets/cur-web/internal/jobs"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func ctxWithFullSession(email, name, role string) context.Context {
	return context.WithValue(context.Background(), auth.ExportedCtxKey{}, &auth.Session{
		Email: email,
		Name:  name,
		Role:  role,
	})
}

func TestPhase4_IsolationAndPreferencesAndQuotas(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	jobsDir := t.TempDir()
	adminEmails := []string{"admin@facets.cloud"}
	spawner := jobs.NewSpawner(jobs.SpawnerConfig{})
	spawner.SetDB(d)
	h := jobs.NewHandler(d, jobsDir, spawner, nil, jobs.NotifyConfig{}, adminEmails)

	aliceEmail := "alice@company.com"
	bobEmail := "bob@competitor.com"
	adminEmail := "admin@facets.cloud"

	require.NoError(t, d.UpsertUserSeen(aliceEmail, "viewer"))
	require.NoError(t, d.UpsertUserSeen(bobEmail, "viewer"))
	require.NoError(t, d.UpsertUserSeen(adminEmail, "admin"))

	aliceJobID := "00000000-0000-0000-0000-000000000044"
	require.NoError(t, d.CreateJob(aliceJobID, aliceEmail, "AliceCorp", ".csv", "sess-1"))

	t.Run("Rule 1: Unauthorized viewer sees 404 Not Found on all endpoints, never 403", func(t *testing.T) {
		endpoints := []struct {
			name   string
			method string
			url    string
			fn     func(http.ResponseWriter, *http.Request)
		}{
			{"GetByID", "GET", "/api/jobs/" + aliceJobID, h.GetByID},
			{"Retry", "POST", "/api/jobs/" + aliceJobID + "/retry", h.Retry},
			{"RegenerateReport", "POST", "/api/jobs/" + aliceJobID + "/regenerate-report", h.RegenerateReport},
			{"Refine", "POST", "/api/jobs/" + aliceJobID + "/refine", h.Refine},
			{"ShareJob", "POST", "/api/jobs/" + aliceJobID + "/share", h.ShareJob},
			{"Runs", "GET", "/api/jobs/" + aliceJobID + "/runs", h.Runs},
			{"Summary", "GET", "/api/jobs/" + aliceJobID + "/summary", h.Summary},
			{"Progress", "GET", "/api/jobs/" + aliceJobID + "/progress", h.Progress},
			{"Cancel", "POST", "/api/jobs/" + aliceJobID + "/cancel", h.Cancel},
			{"Download", "GET", "/api/jobs/" + aliceJobID + "/download", h.Download},
			{"Recalculate", "POST", "/api/jobs/" + aliceJobID + "/recalculate", h.Recalculate},
			{"Delete", "DELETE", "/api/jobs/" + aliceJobID, h.Delete},
		}

		for _, tc := range endpoints {
			t.Run(tc.name, func(t *testing.T) {
				req, _ := http.NewRequestWithContext(ctxWithFullSession(bobEmail, "Bob", "viewer"), tc.method, tc.url, nil)
				req = req.WithContext(withChiID(req.Context(), aliceJobID))
				rec := httptest.NewRecorder()

				tc.fn(rec, req)

				assert.Equal(t, http.StatusNotFound, rec.Code, "endpoint %s should return 404", tc.name)
			})
		}
	})

	t.Run("User Preferences GET and PUT", func(t *testing.T) {
		req, _ := http.NewRequestWithContext(ctxWithFullSession(aliceEmail, "Alice", "viewer"), "GET", "/api/user/preferences", nil)
		rec := httptest.NewRecorder()
		h.GetUserPreferences(rec, req)

		assert.Equal(t, http.StatusOK, rec.Code)
		var prefs map[string]interface{}
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &prefs))
		assert.Equal(t, "gemini-3.8-flash", prefs["default_engine"])

		updatePayload := map[string]interface{}{
			"default_engine":        "gemini-2.5-pro",
			"default_export_format": "xlsx",
			"email_notifications":   false,
		}
		body, _ := json.Marshal(updatePayload)
		putReq, _ := http.NewRequestWithContext(ctxWithFullSession(aliceEmail, "Alice", "viewer"), "PUT", "/api/user/preferences", bytes.NewReader(body))
		putRec := httptest.NewRecorder()
		h.UpdateUserPreferences(putRec, putReq)

		assert.Equal(t, http.StatusOK, putRec.Code)
		var updatedPrefs map[string]interface{}
		require.NoError(t, json.Unmarshal(putRec.Body.Bytes(), &updatedPrefs))
		assert.Equal(t, "gemini-2.5-pro", updatedPrefs["default_engine"])
		assert.Equal(t, "xlsx", updatedPrefs["default_export_format"])
		assert.Equal(t, false, updatedPrefs["email_notifications"])
	})

	t.Run("Quotas: Daily and Concurrent Job Limits with 429", func(t *testing.T) {
		_, _ = d.UpdateReportSetting("max_jobs_per_day", "2", "admin@facets.cloud")
		_, _ = d.UpdateReportSetting("max_concurrent_jobs", "1", "admin@facets.cloud")

		createJobReq := func(userEmail, prospect string) *httptest.ResponseRecorder {
			var body bytes.Buffer
			writer := multipart.NewWriter(&body)
			_ = writer.WriteField("prospect_name", prospect)
			part, _ := writer.CreateFormFile("file", "test.csv")
			_, _ = io.WriteString(part, "lineItem/ProductCode,lineItem/UnblendedCost\nAWS,10.0\n")
			_ = writer.Close()

			req, _ := http.NewRequestWithContext(ctxWithFullSession(userEmail, "User", "viewer"), "POST", "/api/jobs", &body)
			req.Header.Set("Content-Type", writer.FormDataContentType())
			rec := httptest.NewRecorder()
			h.Create(rec, req)
			return rec
		}

		rec1 := createJobReq(bobEmail, "BobCorp1")
		assert.Equal(t, http.StatusCreated, rec1.Code)

		// Job 2 should hit concurrent limit (since job 1 is running/pending)
		rec2 := createJobReq(bobEmail, "BobCorp2")
		assert.Equal(t, http.StatusTooManyRequests, rec2.Code)
		assert.Contains(t, rec2.Body.String(), "concurrent job limit reached")

		// Finish job 1
		var created1 map[string]string
		_ = json.Unmarshal(rec1.Body.Bytes(), &created1)
		time.Sleep(50 * time.Millisecond)
		d.UpdateJobDone(created1["id"], 10.0)

		// Job 2 retry should now succeed
		rec2Retry := createJobReq(bobEmail, "BobCorp2")
		assert.Equal(t, http.StatusCreated, rec2Retry.Code)

		var created2 map[string]string
		_ = json.Unmarshal(rec2Retry.Body.Bytes(), &created2)
		time.Sleep(50 * time.Millisecond)
		d.UpdateJobDone(created2["id"], 10.0)

		// Job 3 should hit daily limit (max 2 daily)
		rec3 := createJobReq(bobEmail, "BobCorp3")
		assert.Equal(t, http.StatusTooManyRequests, rec3.Code)
		assert.Contains(t, rec3.Body.String(), "daily job quota exceeded")

		// Admin user is exempt from quotas
		adminRec := createJobReq(adminEmail, "AdminCorp")
		assert.Equal(t, http.StatusCreated, adminRec.Code)
	})
}
