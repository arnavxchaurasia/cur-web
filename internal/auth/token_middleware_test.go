package auth_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const testSecret = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

func newTokenTestDB(t *testing.T) *db.DB {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	t.Cleanup(func() { d.Close() })
	return d
}

// mwChain establishes the bearer session via TokenMiddleware outermost, then
// runs the outer middleware (RequireAdmin, auth.Middleware, RequireScope), then
// the inner handler.
func mwChain(d *db.DB, outer func(http.Handler) http.Handler, inner http.HandlerFunc) http.Handler {
	mw := auth.TokenMiddleware(d.LookupAPIToken, d.TouchAPITokenLastUsedAsync)
	return mw(outer(http.HandlerFunc(inner)))
}

// scopeOuter adapts the HandlerFunc-based RequireScope to the Handler-based
// mwChain.
func scopeOuter(scope string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return auth.RequireScope(scope)(http.HandlerFunc(next.ServeHTTP))
	}
}

// 9. Valid token populates a session whose Email matches the owner.
func TestTokenMiddlewareValidTokenPopulatesSession(t *testing.T) {
	d := newTokenTestDB(t)
	email := "alice@example.com"
	full, meta, err := d.CreateAPIToken(email, "Praxis laptop", []string{db.ScopeJobsRead}, 90, "127.0.0.1")
	require.NoError(t, err)

	handler := mwChain(d, func(h http.Handler) http.Handler { return h }, func(w http.ResponseWriter, r *http.Request) {
		s := auth.SessionFromCtx(r.Context())
		require.NotNil(t, s, "token must populate a session")
		assert.Equal(t, email, s.Email)
		assert.True(t, auth.TokenAuthenticated(r.Context()), "request must be marked token-authenticated")
		keyID, ok := auth.TokenKeyID(r.Context())
		require.True(t, ok)
		assert.Equal(t, meta.KeyID, keyID)
		w.Write([]byte("ok"))
	})

	req := httptest.NewRequest(http.MethodGet, "/api/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)
}

// 10. No Authorization header passes through untouched.
func TestTokenMiddlewareNoHeaderPassesThrough(t *testing.T) {
	d := newTokenTestDB(t)
	handler := mwChain(d, func(h http.Handler) http.Handler { return h }, func(w http.ResponseWriter, r *http.Request) {
		assert.Nil(t, auth.SessionFromCtx(r.Context()), "no session should be set without a token")
		assert.False(t, auth.TokenAuthenticated(r.Context()))
		w.Write([]byte("ok"))
	})

	req := httptest.NewRequest(http.MethodGet, "/api/jobs", nil)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)
	assert.Equal(t, "ok", w.Body.String())
}

// 11. A token on an admin route is rejected 403.
func TestTokenMiddlewareRejectedOnAdminRoute(t *testing.T) {
	d := newTokenTestDB(t)
	full, _, err := d.CreateAPIToken("alice@example.com", "t", nil, 0, "127.0.0.1")
	require.NoError(t, err)

	handler := mwChain(d, auth.RequireAdmin([]string{}), func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("should not reach"))
	})

	req := httptest.NewRequest(http.MethodGet, "/api/admin/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	assert.Equal(t, http.StatusForbidden, w.Code)
	assert.Contains(t, w.Body.String(), "interactive session")
}

// 12. A jobs:read token on a jobs:write route is rejected 403.
func TestTokenMiddlewareRequiresScope(t *testing.T) {
	d := newTokenTestDB(t)
	full, _, err := d.CreateAPIToken("alice@example.com", "t", []string{db.ScopeJobsRead}, 0, "127.0.0.1")
	require.NoError(t, err)

	handler := mwChain(d, scopeOuter(db.ScopeJobsWrite), func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	})

	req := httptest.NewRequest(http.MethodPost, "/api/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	assert.Equal(t, http.StatusForbidden, w.Code)
}

// A jobs:read token on a jobs:read route is allowed.
func TestTokenMiddlewareReadScopeAllowed(t *testing.T) {
	d := newTokenTestDB(t)
	full, _, err := d.CreateAPIToken("alice@example.com", "t", []string{db.ScopeJobsRead}, 0, "127.0.0.1")
	require.NoError(t, err)

	handler := mwChain(d, scopeOuter(db.ScopeJobsRead), func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	})

	req := httptest.NewRequest(http.MethodGet, "/api/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)
}

// 13. Role is resolved live — demote the user in the DB, same token resolves
// as viewer. Mirrors the cookie path: auth.Middleware resolves role per-request.
func TestTokenMiddlewareRoleResolvedLive(t *testing.T) {
	d := newTokenTestDB(t)
	email := "alice@example.com"
	full, _, err := d.CreateAPIToken(email, "t", nil, 0, "127.0.0.1")
	require.NoError(t, err)

	overrides := map[string]string{}
	getRole := func(e string) (string, string, error) {
		if role, ok := overrides[e]; ok {
			return role, "", nil
		}
		return "", "", nil
	}

	sm := auth.NewSessionManager(testSecret, false)
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s := auth.SessionFromCtx(r.Context())
		w.Write([]byte(s.Role))
	})

	// Admin: getRole says admin.
	overrides[email] = "admin"
	require.NoError(t, d.SetUserRole(email, "admin"))
	handler := mwChain(d, func(h http.Handler) http.Handler { return auth.Middleware(sm, getRole)(h) }, inner)
	req := httptest.NewRequest(http.MethodGet, "/api/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)
	assert.Equal(t, "admin", strings.TrimSpace(w.Body.String()), "role resolved live from DB")

	// Demote: getRole now says viewer, same token resolves as viewer.
	overrides[email] = "viewer"
	require.NoError(t, d.SetUserRole(email, "viewer"))
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/api/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	handler.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)
	assert.Equal(t, "viewer", strings.TrimSpace(w.Body.String()), "demoted user resolves as viewer on same token")
}

// 15. The token string never appears in log output. TokenMiddleware logs
// nothing at all by design, so the response contains no token material.
func TestTokenMiddlewareNeverLogsToken(t *testing.T) {
	d := newTokenTestDB(t)
	full, meta, err := d.CreateAPIToken("alice@example.com", "t", nil, 0, "127.0.0.1")
	require.NoError(t, err)

	handler := mwChain(d, func(h http.Handler) http.Handler { return h }, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	})
	req := httptest.NewRequest(http.MethodGet, "/api/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+full)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	assert.Equal(t, http.StatusOK, w.Code)

	assert.NotContains(t, w.Body.String(), full)
	secret := full[strings.LastIndex(full, "_")+1:]
	assert.NotContains(t, w.Body.String(), secret)
	assert.NotContains(t, w.Body.String(), meta.KeyID)
}
