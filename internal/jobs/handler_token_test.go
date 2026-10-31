package jobs_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/facets/cur-web/internal/jobs"
	"github.com/go-chi/chi/v5"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// buildTokenTestRouter mounts the token routes with the same middleware stack
// as main.go: TokenMiddleware, APICacheControl, session auth.
func buildTokenTestRouter(database *db.DB, jobsDir string) http.Handler {
	sm := auth.NewSessionManager("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", false)
	spawner := jobs.NewSpawner(jobs.SpawnerConfig{})
	spawner.SetDB(database)
	jobHandler := jobs.NewHandler(database, jobsDir, spawner, nil, jobs.NotifyConfig{}, nil)

	r := chi.NewRouter()
	r.Use(auth.TokenMiddleware(database.LookupAPIToken, database.TouchAPITokenLastUsedAsync))

	r.Group(func(r chi.Router) {
		r.Use(auth.APICacheControl)
		r.Use(func(next http.Handler) http.Handler {
			return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				// A bearer-token middleware may have populated a session in the
				// context; otherwise fall back to the session cookie.
				sess := auth.SessionFromCtx(req.Context())
				if sess == nil {
					s, err := sm.Get(req)
					if err != nil {
						http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
						return
					}
					sess = s
				}
				if role, _, err := database.GetUserRoleAndGrant(sess.Email); err == nil && role != "" {
					sess.Role = role
				}
				next.ServeHTTP(w, req.WithContext(contextWithSession(req.Context(), sess)))
			})
		})
		r.Get("/api/user/tokens", jobHandler.ListUserAPITokens)
		r.Post("/api/user/tokens", jobHandler.CreateUserAPIToken)
		r.Delete("/api/user/tokens/{key_id}", jobHandler.RevokeUserAPIToken)
	})
	return r
}

func contextWithSession(ctx context.Context, sess *auth.Session) context.Context {
	return context.WithValue(ctx, auth.ExportedCtxKey{}, sess)
}

func sessionCookie(t *testing.T, sm *auth.SessionManager, email string) string {
	sess := &auth.Session{Email: email, Role: "viewer"}
	w := httptest.NewRecorder()
	require.NoError(t, sm.Set(w, sess))
	return w.Result().Header.Get("Set-Cookie")
}

// Creates a token, asserts the secret is returned once, Cache-Control is
// no-store, and the DB never holds the secret.
func TestCreateListRevokeAPIToken(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	sm := auth.NewSessionManager("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", false)
	router := buildTokenTestRouter(d, t.TempDir())
	email := "alice@example.com"
	cookie := sessionCookie(t, sm, email)

	// POST create
	body := strings.NewReader(`{"name":"Praxis laptop","scopes":["jobs:read"],"ttl_days":90}`)
	req := httptest.NewRequest(http.MethodPost, "/api/user/tokens", body)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Cookie", cookie)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	assert.Equal(t, http.StatusCreated, w.Code)
	assert.Contains(t, w.Header().Get("Cache-Control"), "no-store")

	var created struct {
		Token string `json:"token"`
		Meta  struct {
			KeyID   string   `json:"key_id"`
			Scopes  []string `json:"scopes"`
			Display string   `json:"display"`
		} `json:"meta"`
	}
	require.NoError(t, json.NewDecoder(w.Body).Decode(&created))
	assert.True(t, strings.HasPrefix(created.Token, "fct_live_"), "token must start with fct_live_")
	assert.Equal(t, []string{"jobs:read"}, created.Meta.Scopes)
	assert.Equal(t, "fct_live_"+created.Meta.KeyID+"_…", created.Meta.Display)

	// The secret must not be stored anywhere in the DB.
	var count int
	require.NoError(t, d.QueryRowForTest(`SELECT COUNT(*) FROM api_tokens WHERE token_hash = ? OR key_id = ?`, created.Token, created.Token).Scan(&count))
	assert.Equal(t, 0, count)

	// GET list
	req = httptest.NewRequest(http.MethodGet, "/api/user/tokens", nil)
	req.Header.Set("Cookie", cookie)
	w = httptest.NewRecorder()
	router.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)
	assert.Contains(t, w.Header().Get("Cache-Control"), "no-store")
	var listed struct {
		Tokens []struct {
			KeyID   string `json:"key_id"`
			Name    string `json:"name"`
			Display string `json:"display"`
		} `json:"tokens"`
	}
	require.NoError(t, json.NewDecoder(w.Body).Decode(&listed))
	require.Len(t, listed.Tokens, 1)
	assert.Equal(t, created.Meta.KeyID, listed.Tokens[0].KeyID)
	assert.Equal(t, "Praxis laptop", listed.Tokens[0].Name)

	// DELETE revoke
	req = httptest.NewRequest(http.MethodDelete, "/api/user/tokens/"+created.Meta.KeyID, nil)
	req.Header.Set("Cookie", cookie)
	w = httptest.NewRecorder()
	router.ServeHTTP(w, req)
	assert.Equal(t, http.StatusNoContent, w.Code)

	// Revoked token no longer resolves.
	_, err = d.LookupAPIToken(created.Token)
	assert.ErrorIs(t, err, db.ErrTokenRevoked)
}

// Token-authenticated requests cannot manage tokens (403).
func TestTokenCannotManageTokens(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	router := buildTokenTestRouter(d, t.TempDir())
	email := "alice@example.com"
	full, _, err := d.CreateAPIToken(email, "t", nil, 0, "127.0.0.1")
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/api/user/tokens", strings.NewReader(`{"name":"x"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	assert.Equal(t, http.StatusForbidden, w.Code)
}
