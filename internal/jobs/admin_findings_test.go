package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/go-chi/chi/v5"
)

func setupTestDBWithFindings(t *testing.T) (*db.DB, string) {
	tmpDir := t.TempDir()
	dbPath := filepath.Join(tmpDir, "test.db")
	database, err := db.Open(dbPath)
	if err != nil {
		t.Fatalf("db.Open failed: %v", err)
	}

	finding := db.ReviewFinding{
		ID:               "find-123",
		JobID:            "job-abc",
		AWSLIKey:         "key-xyz",
		CanonicalService: "custom_service_bar",
		ChargeType:       "usage",
		FindingType:      "passthrough_should_be_mapped",
		Severity:         "HIGH",
		AWSOD:            150.0,
		GCPOD:            0.0,
		Ratio:            0.0,
		SuggestedAction:  "Map to Cloud Run",
		Status:           "pending",
		Evidence: map[string]any{
			"product":     "Custom Service Bar",
			"aws_od":      150.0,
			"gcp_service": "Cloud Run",
		},
	}

	if err := database.SaveReviewFindings([]db.ReviewFinding{finding}); err != nil {
		t.Fatalf("SaveReviewFindings failed: %v", err)
	}

	return database, tmpDir
}

func TestAdminFindingsEndpoints(t *testing.T) {
	database, tmpDir := setupTestDBWithFindings(t)
	defer database.Close()

	// Ensure fake skill data dir for refresh store
	fakeSkillDir := filepath.Join(tmpDir, "fake_skill")
	fakeDataDir := filepath.Join(fakeSkillDir, "data")
	os.MkdirAll(fakeDataDir, 0755)
	t.Setenv("SKILL_DIR", fakeSkillDir)

	h := &Handler{
		db: database,
	}

	r := chi.NewRouter()
	r.Use(func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			sess := &auth.Session{
				Email: "admin@example.com",
				Role:  "admin",
			}
			ctx := context.WithValue(req.Context(), auth.ExportedCtxKey{}, sess)
			next.ServeHTTP(w, req.WithContext(ctx))
		})
	})
	r.Get("/api/admin/findings", h.AdminListFindings)
	r.Get("/api/admin/jobs/{id}/findings", h.AdminListFindings)
	r.Post("/api/admin/findings/{id}/action", h.AdminActionFinding)

	// 1. GET /api/admin/findings
	req := httptest.NewRequest("GET", "/api/admin/findings", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/admin/findings status %d != 200: %s", rec.Code, rec.Body.String())
	}
	var listResp struct {
		Findings []db.ReviewFinding `json:"findings"`
		Total    int                `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &listResp); err != nil {
		t.Fatalf("json unmarshal failed: %v", err)
	}
	if listResp.Total != 1 || len(listResp.Findings) != 1 {
		t.Fatalf("expected 1 finding, got %d", listResp.Total)
	}
	if listResp.Findings[0].ID != "find-123" {
		t.Fatalf("unexpected finding ID: %s", listResp.Findings[0].ID)
	}

	// 2. POST /api/admin/findings/{id}/action (accept)
	actionBody := map[string]any{
		"action":       "accept",
		"note":         "Confirmed mapping to Cloud Run",
		"gcp_service":  "Cloud Run",
		"gcp_sku_name": "Cloud Run Requests",
		"strategy":     "map",
	}
	bodyBytes, _ := json.Marshal(actionBody)
	req2 := httptest.NewRequest("POST", "/api/admin/findings/find-123/action", bytes.NewReader(bodyBytes))
	rec2 := httptest.NewRecorder()
	r.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusOK {
		t.Fatalf("POST action status %d != 200: %s", rec2.Code, rec2.Body.String())
	}

	// 3. Verify status updated in DB
	f, err := database.GetReviewFindingByID("find-123")
	if err != nil || f == nil {
		t.Fatalf("failed to get finding after action: %v", err)
	}
	if f.Status != "accepted" {
		t.Fatalf("expected status 'accepted', got '%s'", f.Status)
	}

	// 4. Verify refresh store was updated
	storePath := filepath.Join(fakeDataDir, "refresh_store.json")
	if _, err := os.Stat(storePath); os.IsNotExist(err) {
		t.Fatalf("expected refresh_store.json to be created at %s", storePath)
	}
	storeData, _ := os.ReadFile(storePath)
	var storeDoc struct {
		Mappings map[string]struct {
			GCPService string `json:"gcp_service"`
			Status     string `json:"status"`
		} `json:"mappings"`
	}
	if err := json.Unmarshal(storeData, &storeDoc); err != nil {
		t.Fatalf("unmarshal refresh store failed: %v", err)
	}
	entry, ok := storeDoc.Mappings["custom_service_bar"]
	if !ok {
		t.Fatalf("expected custom_service_bar in refresh store mappings, got %v", storeDoc.Mappings)
	}
	if entry.GCPService != "Cloud Run" || entry.Status != "trusted" {
		t.Fatalf("unexpected entry in refresh store: %+v", entry)
	}
}
