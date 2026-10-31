package auth

import (
	"context"
	"encoding/json"
	"net/http"
)

// ExportedCtxKey is exported so handler tests in other packages can inject sessions.
type ExportedCtxKey struct{}

// getRoleFunc looks up a user's current RBAC role from the durable users table.
// It can optionally return (role, adminGrantedBy, error) or (role, error).
// We accept any function matching the lookup signature.
type getRoleFunc any

func Middleware(sm *SessionManager, getRole getRoleFunc, devBypass ...bool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// A bearer-token middleware may have already populated a session in
			// the request context (see TokenMiddleware). In that case skip the
			// cookie requirement — the session is the session regardless of how
			// it was established.
			sess := SessionFromCtx(r.Context())
			if sess == nil {
				s, err := sm.Get(r)
				if err != nil {
					http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
					return
				}
				sess = s
			}
			// Resolve role from the users table on every request so that role
			// changes (admin demotion/promotion) take effect immediately without
			// requiring the user to log out and back in.
			if getRole != nil {
				switch fn := getRole.(type) {
				case func(email string) (string, string, error):
					if role, grantedBy, err := fn(sess.Email); err == nil && role != "" {
						sess.Role = role
						sess.AdminGrantedBy = grantedBy
					}
				case func(email string) (string, error):
					if role, err := fn(sess.Email); err == nil && role != "" {
						sess.Role = role
					}
				}
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
			// Admin routes must reject bearer tokens entirely: a long-lived
			// credential is too much blast radius for the admin surface.
			if TokenAuthenticated(r.Context()) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]string{"error": "admin routes require an interactive session"})
				return
			}
			if !sess.IsAdmin(adminEmails) {
				http.Error(w, `{"error":"forbidden"}`, http.StatusForbidden)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
