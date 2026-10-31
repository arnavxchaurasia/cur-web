package auth

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/facets/cur-web/internal/db"
)

// tokenAuthCtxKey marks a request authenticated by a bearer token (not an
// interactive session cookie). Admin routes must reject these.
type tokenAuthCtxKey struct{}

// tokenKeyIDCtxKey carries the key id of the token that authenticated the
// request, used for per-key-id rate limiting.
type tokenKeyIDCtxKey struct{}

// tokenScopesCtxKey carries the token's scopes, used by RequireScope.
type tokenScopesCtxKey struct{}

// TokenAuthenticated reports whether the request was authenticated by a bearer
// token rather than an interactive session cookie.
func TokenAuthenticated(ctx context.Context) bool {
	_, ok := ctx.Value(tokenAuthCtxKey{}).(bool)
	return ok
}

// TokenKeyID returns the key id of the token that authenticated the request,
// if any.
func TokenKeyID(ctx context.Context) (string, bool) {
	k, ok := ctx.Value(tokenKeyIDCtxKey{}).(string)
	return k, ok
}

// TokenScopes returns the scopes of the token that authenticated the request,
// if any.
func TokenScopes(ctx context.Context) ([]string, bool) {
	s, ok := ctx.Value(tokenScopesCtxKey{}).([]string)
	return s, ok
}

// withTokenAuth returns a context marked as token-authenticated with the given
// key id and scopes.
func withTokenAuth(ctx context.Context, keyID string, scopes []string) context.Context {
	ctx = context.WithValue(ctx, tokenAuthCtxKey{}, true)
	ctx = context.WithValue(ctx, tokenKeyIDCtxKey{}, keyID)
	return context.WithValue(ctx, tokenScopesCtxKey{}, scopes)
}

// WithTokenAuthForTest marks a context as token-authenticated with the given
// key id and scopes. Exported for tests in other packages (e.g. the MCP
// server) that need to simulate a bearer-token request.
func WithTokenAuthForTest(ctx context.Context, keyID string, scopes []string) context.Context {
	return withTokenAuth(ctx, keyID, scopes)
}

// TokenMiddleware authenticates "Authorization: Bearer fct_live_..." and
// populates the same *Session a cookie login would. Requests with no
// Authorization header pass through untouched for the cookie middleware.
//
// On a valid token it builds a *Session (email from the token row; role is
// resolved live by auth.Middleware) and marks the request token-authenticated
// so admin routes can reject it. It never logs the token string, only the key
// id.
func TokenMiddleware(lookup func(string) (*db.APIToken, error), touch func(string)) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			authHeader := r.Header.Get("Authorization")
			if authHeader == "" {
				next.ServeHTTP(w, r)
				return
			}
			// Header present but not "Bearer " -> 401. A client that sent a
			// credential deserves to be told it was rejected.
			const bearer = "Bearer "
			if !strings.HasPrefix(authHeader, bearer) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnauthorized)
				_ = json.NewEncoder(w).Encode(map[string]string{"error": "unauthorized"})
				return
			}
			presented := strings.TrimPrefix(authHeader, bearer)

			token, err := lookup(presented)
			if err != nil {
				// ErrTokenNotFound covers malformed, unknown, and mismatched
				// tokens — deliberately indistinguishable so a caller cannot
				// probe for valid key ids. Revoked and expired get their own
				// messages because the holder proved possession.
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnauthorized)
				msg := "invalid token"
				switch err {
				case db.ErrTokenRevoked:
					msg = "token revoked"
				case db.ErrTokenExpired:
					msg = "token expired"
				}
				_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
				return
			}

			// Valid token. Build the session the same way a cookie login would;
			// auth.Middleware resolves the role live on every request.
			sess := &Session{
				Email: token.UserEmail,
				Name:  token.Name,
			}
			ctx := withTokenAuth(r.Context(), token.KeyID, token.Scopes)
			ctx = context.WithValue(ctx, ExportedCtxKey{}, sess)

			// Refresh last_used_at off the request path; never block the caller.
			if touch != nil {
				go touch(token.KeyID)
			}

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// RequireScope returns middleware that rejects token-authenticated requests
// whose token lacks the given scope. Cookie-authenticated requests pass
// unconditionally: the session is the full-privilege credential and scopes are
// a token concept. This is intended for the token-reachable REST routes.
func RequireScope(scope string) func(http.HandlerFunc) http.HandlerFunc {
	return func(next http.HandlerFunc) http.HandlerFunc {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !TokenAuthenticated(r.Context()) {
				next.ServeHTTP(w, r)
				return
			}
			allowed := false
			if scopes, ok := TokenScopes(r.Context()); ok {
				for _, s := range scopes {
					if s == scope {
						allowed = true
						break
					}
				}
			}
			if !allowed {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusForbidden)
				_ = json.NewEncoder(w).Encode(map[string]string{"error": "insufficient scope"})
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// rateBucket is a per-key-id token bucket used to rate-limit token-authenticated
// API calls.
type rateBucket struct {
	tokens float64
	last   time.Time
	mu     sync.Mutex
}

var (
	tokenRateMu          sync.Mutex
	tokenRateBuckets     = make(map[string]*rateBucket)
	tokenRateLimitPerS   = 600.0 / 60.0 // 600/minute == 10/second
	tokenRateBurst       = 600.0
)

// TokenRateLimitMiddleware rate-limits token-authenticated API calls to
// 600/minute per key id, returning 429 with a Retry-After header. Cookie-
// authenticated requests pass through unconditionally.
func TokenRateLimitMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !TokenAuthenticated(r.Context()) {
			next.ServeHTTP(w, r)
			return
		}
		keyID, _ := TokenKeyID(r.Context())
		tokenRateMu.Lock()
		b, ok := tokenRateBuckets[keyID]
		if !ok {
			b = &rateBucket{tokens: tokenRateBurst, last: time.Now()}
			tokenRateBuckets[keyID] = b
		}
		tokenRateMu.Unlock()
		if !b.allow() {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "rate limit exceeded"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (b *rateBucket) allow() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	now := time.Now()
	elapsed := now.Sub(b.last).Seconds()
	b.last = now
	b.tokens += elapsed * tokenRateLimitPerS
	if b.tokens > tokenRateBurst {
		b.tokens = tokenRateBurst
	}
	if b.tokens < 1.0 {
		return false
	}
	b.tokens--
	return true
}

// APICacheControl sets no-store cache headers on every /api/ response so
// per-user data (and access tokens in particular) is never cached by a
// browser, intermediary proxy, or CDN. Vary: Cookie, Authorization ensures
// responses differ by credentials — this fixes the reported "same token for
// different users" symptom.
func APICacheControl(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store, must-revalidate, private")
		w.Header().Set("Pragma", "no-cache")
		w.Header().Set("Vary", "Cookie, Authorization")
		next.ServeHTTP(w, r)
	})
}
