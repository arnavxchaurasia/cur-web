package auth

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

var ErrNoSession = errors.New("no session")

const cookieName = "cur_session"
const cookieTTL = 8 * time.Hour

type Session struct {
	Email    string `json:"email"`
	Name     string `json:"name"`
	IssuedAt int64  `json:"iat,omitempty"`
}

// IsAdmin reports whether this session's email is in the admin allow-list.
// adminEmails entries are expected to be lowercased already (parseAdminEmails
// does that at startup). Comparison is case-insensitive on the session side.
func (s *Session) IsAdmin(adminEmails []string) bool {
	if s == nil {
		return false
	}
	email := strings.ToLower(s.Email)
	for _, a := range adminEmails {
		if a == email {
			return true
		}
	}
	return false
}

type SessionManager struct {
	key    []byte
	secure bool
}

func NewSessionManager(hexKey string, secure bool) *SessionManager {
	key, err := base64.StdEncoding.DecodeString(hexKey)
	if err != nil || len(key) == 0 {
		// Issue 5: log the fallback so misconfigured SESSION_SECRET is visible
		slog.Warn("SESSION_SECRET is not valid base64; using raw bytes as AES key")
		key = []byte(hexKey)
	}
	if len(key) < 32 {
		panic("SESSION_SECRET must decode to at least 32 bytes — use a base64-encoded 32-byte random value")
	}
	if len(key) > 32 {
		// Issue 9: warn rather than silently drop the extra bytes
		slog.Warn("SESSION_SECRET decodes to more than 32 bytes; truncating to 32 for AES-256", "decoded_len", len(key))
	}
	return &SessionManager{key: key[:32], secure: secure}
}

func (sm *SessionManager) Secure() bool { return sm.secure }

func (sm *SessionManager) Set(w http.ResponseWriter, s *Session) error {
	// Issue 1: stamp issue time so Get() can enforce server-side TTL
	stamped := *s
	stamped.IssuedAt = time.Now().Unix()
	data, err := json.Marshal(stamped)
	if err != nil {
		return err
	}
	block, err := aes.NewCipher(sm.key)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err = io.ReadFull(rand.Reader, nonce); err != nil {
		return err
	}
	ciphertext := gcm.Seal(nonce, nonce, data, nil)
	val := base64.URLEncoding.EncodeToString(ciphertext)
	http.SetCookie(w, &http.Cookie{
		Name:     cookieName,
		Value:    val,
		Path:     "/",
		HttpOnly: true,
		Secure:   sm.secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   int(cookieTTL.Seconds()),
	})
	return nil
}

func (sm *SessionManager) Get(r *http.Request) (*Session, error) {
	c, err := r.Cookie(cookieName)
	if err != nil {
		return nil, ErrNoSession
	}
	ciphertext, err := base64.URLEncoding.DecodeString(c.Value)
	if err != nil {
		return nil, ErrNoSession
	}
	block, err := aes.NewCipher(sm.key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	ns := gcm.NonceSize()
	if len(ciphertext) < ns {
		return nil, ErrNoSession
	}
	plain, err := gcm.Open(nil, ciphertext[:ns], ciphertext[ns:], nil)
	if err != nil {
		return nil, ErrNoSession
	}
	var s Session
	if err := json.Unmarshal(plain, &s); err != nil {
		return nil, ErrNoSession
	}
	// Issue 1: enforce server-side TTL; browser MaxAge is advisory only
	if s.IssuedAt == 0 || time.Since(time.Unix(s.IssuedAt, 0)) > cookieTTL {
		return nil, ErrNoSession
	}
	return &s, nil
}

func (sm *SessionManager) Clear(w http.ResponseWriter) {
	// Issue 3: mirror Set()'s flags so browsers match and clear the cookie reliably
	http.SetCookie(w, &http.Cookie{
		Name:     cookieName,
		MaxAge:   -1,
		Path:     "/",
		HttpOnly: true,
		Secure:   sm.secure,
		SameSite: http.SameSiteLaxMode,
	})
}
