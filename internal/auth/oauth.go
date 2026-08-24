package auth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
)

type OAuthHandler struct {
	cfg            *oauth2.Config
	sm             *SessionManager
	redirectAfter  string
	devBypass      bool
	allowedDomains map[string]bool
	adminEmails    []string
}

func NewOAuthHandler(clientID, clientSecret, redirectURI string, sm *SessionManager, appBaseURL string, devBypass bool, allowedDomains, adminEmails []string) *OAuthHandler {
	allowed := make(map[string]bool, len(allowedDomains))
	for _, d := range allowedDomains {
		allowed[d] = true
	}
	return &OAuthHandler{
		cfg: &oauth2.Config{
			ClientID:     clientID,
			ClientSecret: clientSecret,
			RedirectURL:  redirectURI,
			Scopes:       []string{"openid", "email", "profile"},
			Endpoint:     google.Endpoint,
		},
		sm:             sm,
		redirectAfter:  appBaseURL,
		devBypass:      devBypass,
		allowedDomains: allowed,
		adminEmails:    adminEmails,
	}
}

func (h *OAuthHandler) Login(w http.ResponseWriter, r *http.Request) {
	if h.devBypass {
		h.sm.Set(w, &Session{Email: "dev@google.com", Name: "Dev User"})
		http.Redirect(w, r, "/", http.StatusTemporaryRedirect)
		return
	}
	state := randomState()
	http.SetCookie(w, &http.Cookie{Name: "oauth_state", Value: state, HttpOnly: true, Secure: h.sm.Secure(), MaxAge: 600})
	// Drop the `hd` hint when multiple domains are allowed — Google's hd
	// param only accepts a single value. Domain enforcement happens server-
	// side in Callback.
	var url string
	if len(h.allowedDomains) == 1 {
		for d := range h.allowedDomains {
			url = h.cfg.AuthCodeURL(state, oauth2.SetAuthURLParam("hd", d))
		}
	} else {
		url = h.cfg.AuthCodeURL(state)
	}
	http.Redirect(w, r, url, http.StatusTemporaryRedirect)
}

func (h *OAuthHandler) Callback(w http.ResponseWriter, r *http.Request) {
	stateCookie, err := r.Cookie("oauth_state")
	if err != nil || stateCookie.Value != r.URL.Query().Get("state") {
		http.Error(w, "invalid state", http.StatusBadRequest)
		return
	}
	token, err := h.cfg.Exchange(context.Background(), r.URL.Query().Get("code"))
	if err != nil {
		http.Error(w, "token exchange failed", http.StatusInternalServerError)
		return
	}
	client := h.cfg.Client(context.Background(), token)
	resp, err := client.Get("https://www.googleapis.com/oauth2/v3/userinfo")
	if err != nil {
		http.Error(w, "userinfo failed", http.StatusInternalServerError)
		return
	}
	defer resp.Body.Close()
	var info struct {
		Email string `json:"email"`
		Name  string `json:"name"`
		HD    string `json:"hd"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		http.Error(w, "decode failed", http.StatusInternalServerError)
		return
	}
	if !h.allowedDomains[info.HD] {
		http.Error(w, "forbidden: account domain not allowed", http.StatusForbidden)
		return
	}
	h.sm.Set(w, &Session{Email: info.Email, Name: info.Name})
	http.Redirect(w, r, "/", http.StatusTemporaryRedirect)
}

func (h *OAuthHandler) Logout(w http.ResponseWriter, r *http.Request) {
	h.sm.Clear(w)
	http.Redirect(w, r, "/", http.StatusTemporaryRedirect)
}

func (h *OAuthHandler) Me(w http.ResponseWriter, r *http.Request) {
	sess := SessionFromCtx(r.Context())
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"email":    sess.Email,
		"name":     sess.Name,
		"is_admin": sess.IsAdmin(h.adminEmails),
	})
}

func randomState() string {
	b := make([]byte, 16)
	rand.Read(b)
	return base64.URLEncoding.EncodeToString(b)
}

var ErrForbiddenDomain = errors.New("not a google.com account")
