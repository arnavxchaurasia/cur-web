// internal/jobs/handler_test.go
package jobs_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/facets/cur-web/internal/jobs"
	"github.com/go-chi/chi/v5"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func ctxWithSession(email, name string) context.Context {
	return context.WithValue(context.Background(), auth.ExportedCtxKey{}, &auth.Session{Email: email, Name: name})
}

func withChiID(ctx context.Context, id string) context.Context {
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("id", id)
	return context.WithValue(ctx, chi.RouteCtxKey, rctx)
}

func TestListJobs_Empty(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()
	dir := t.TempDir()
	h := jobs.NewHandler(d, dir, nil, nil, jobs.NotifyConfig{}, nil)

	req, _ := http.NewRequestWithContext(ctxWithSession("rep@google.com", "Rep"), "GET", "/api/jobs", nil)
	w := httptest.NewRecorder()
	h.List(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var result []interface{}
	json.Unmarshal(w.Body.Bytes(), &result)
	assert.Len(t, result, 0)
}

func TestGetJob_NotOwner(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()
	d.CreateJob("j1", "other@google.com", "Acme", ".csv", "")
	dir := t.TempDir()
	h := jobs.NewHandler(d, dir, nil, nil, jobs.NotifyConfig{}, nil)

	ctx := withChiID(ctxWithSession("rep@google.com", "Rep"), "j1")
	req, _ := http.NewRequestWithContext(ctx, "GET", "/api/jobs/j1", nil)
	w := httptest.NewRecorder()
	h.GetByID(w, req)

	assert.Equal(t, http.StatusNotFound, w.Code)
}

func TestRuns_EmptyForLegacyJob(t *testing.T) {
	// A done job without a projection.duckdb (or with no run_results
	// table) must return an empty JSON array — not 404.
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()
	d.CreateJob("j-legacy", "rep@google.com", "Acme", ".csv", "")
	dir := t.TempDir()
	h := jobs.NewHandler(d, dir, nil, nil, jobs.NotifyConfig{}, nil)

	ctx := withChiID(ctxWithSession("rep@google.com", "Rep"), "j-legacy")
	req, _ := http.NewRequestWithContext(ctx, "GET", "/api/jobs/j-legacy/runs", nil)
	w := httptest.NewRecorder()
	h.Runs(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var result []interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &result))
	assert.Len(t, result, 0)
}

func TestRuns_NotOwner(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()
	d.CreateJob("j-other", "other@google.com", "Acme", ".csv", "")
	dir := t.TempDir()
	h := jobs.NewHandler(d, dir, nil, nil, jobs.NotifyConfig{}, nil)

	ctx := withChiID(ctxWithSession("rep@google.com", "Rep"), "j-other")
	req, _ := http.NewRequestWithContext(ctx, "GET", "/api/jobs/j-other/runs", nil)
	w := httptest.NewRecorder()
	h.Runs(w, req)

	assert.Equal(t, http.StatusNotFound, w.Code)
}

func TestSummary_404WhenMissing(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()
	d.CreateJob("j-nosummary", "rep@google.com", "Acme", ".csv", "")
	dir := t.TempDir()
	h := jobs.NewHandler(d, dir, nil, nil, jobs.NotifyConfig{}, nil)

	ctx := withChiID(ctxWithSession("rep@google.com", "Rep"), "j-nosummary")
	req, _ := http.NewRequestWithContext(ctx, "GET", "/api/jobs/j-nosummary/summary", nil)
	w := httptest.NewRecorder()
	h.Summary(w, req)

	assert.Equal(t, http.StatusNotFound, w.Code)
}

func TestGetJob_Owner(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()
	d.CreateJob("j1", "rep@google.com", "Acme Corp", ".csv", "")
	dir := t.TempDir()
	h := jobs.NewHandler(d, dir, nil, nil, jobs.NotifyConfig{}, nil)

	ctx := withChiID(ctxWithSession("rep@google.com", "Rep"), "j1")
	req, _ := http.NewRequestWithContext(ctx, "GET", "/api/jobs/j1", nil)
	w := httptest.NewRecorder()
	h.GetByID(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var job map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &job)
	assert.Equal(t, "Acme Corp", job["prospect"])
}
