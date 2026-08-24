package auth_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestSessionRoundTrip(t *testing.T) {
	secret := "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
	s := auth.NewSessionManager(secret, false)

	sess := &auth.Session{Email: "rep@google.com", Name: "Rep User"}
	w := httptest.NewRecorder()
	err := s.Set(w, sess)
	require.NoError(t, err)

	req := &http.Request{Header: http.Header{"Cookie": w.Result().Header["Set-Cookie"]}}
	got, err := s.Get(req)
	require.NoError(t, err)
	assert.Equal(t, "rep@google.com", got.Email)
	assert.Equal(t, "Rep User", got.Name)
}

func TestSessionGet_NoCookie(t *testing.T) {
	s := auth.NewSessionManager("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", false)
	req, _ := http.NewRequest("GET", "/", nil)
	_, err := s.Get(req)
	assert.ErrorIs(t, err, auth.ErrNoSession)
}
