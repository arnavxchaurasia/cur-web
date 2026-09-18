package auth

import (
	"context"
	"net/http"
)

// ExportedCtxKey is exported so handler tests in other packages can inject sessions.
type ExportedCtxKey struct{}

func Middleware(sm *SessionManager) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			sess, err := sm.Get(r)
			if err != nil {
				http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
				return
			}
			ctx := context.WithValue(r.Context(), ExportedCtxKey{}, sess)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

func SessionFromCtx(ctx context.Context) *Session {
	s, _ := ctx.Value(ExportedCtxKey{}).(*Session)
	return s
}

// RequireAdmin wraps a handler chain (after Middleware) and rejects any
// request whose session isn't an admin (per Session.IsAdmin) with 403.
// Centralizes the inline `sess.IsAdmin(...)` check for new /api/admin/*
// routes instead of repeating it in every handler.
func RequireAdmin(adminEmails []string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			sess := SessionFromCtx(r.Context())
			if !sess.IsAdmin(adminEmails) {
				http.Error(w, `{"error":"forbidden"}`, http.StatusForbidden)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
