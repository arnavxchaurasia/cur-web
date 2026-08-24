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
