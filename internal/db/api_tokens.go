// Package db provides the api_tokens table layer: hashed personal access
// tokens that authenticate Bearer requests without a session cookie.
//
// Tokens are stored hashed (SHA-256 of the secret). Only the key id (12 hex
// chars, cleartext and indexed) is stored alongside, so a presented token can
// be looked up in O(1) rather than hashing the secret against every row. The
// full secret is returned to the caller exactly once and is never stored.
package db

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"
)

// TokenPrefix is the fixed, greppable prefix on every issued token.
const TokenPrefix = "fct_live_"

var (
	ErrTokenNotFound = errors.New("token not found")
	ErrTokenRevoked  = errors.New("token revoked")
	ErrTokenExpired  = errors.New("token expired")
)

// Scope values form a closed set. Scopes narrow what a token can do within
// what its owner can already do; they never widen it.
const (
	ScopeJobsRead    = "jobs:read"    // list own jobs, read own reports
	ScopeJobsWrite   = "jobs:write"   // create and cancel own jobs
	ScopeReportsRead = "reports:read" // download own report artifacts
)

// IsValidScope reports whether s is a member of the closed scope set.
func IsValidScope(s string) bool {
	switch s {
	case ScopeJobsRead, ScopeJobsWrite, ScopeReportsRead:
		return true
	}
	return false
}

// APIToken is a token's metadata. It never carries the secret.
type APIToken struct {
	KeyID      string     `json:"key_id"`
	UserEmail  string     `json:"user_email"`
	Name       string     `json:"name"`
	Scopes     []string   `json:"scopes"`
	CreatedAt  string     `json:"created_at"`
	LastUsedAt *string    `json:"last_used_at"`
	ExpiresAt  *string    `json:"expires_at"`
	RevokedAt  *string    `json:"revoked_at"`
	// Display is the masked form shown in lists, e.g. "fct_live_a1b2c3d4e5f6_…"
	Display string `json:"display"`
}

func hashToken(secret string) string {
	sum := sha256.Sum256([]byte(secret))
	return hex.EncodeToString(sum[:])
}

// CreateAPIToken issues a new token and returns the ONLY copy of the full
// string. The caller must hand it to the user immediately; it is not
// recoverable afterwards because only the SHA-256 hash of the secret is stored.
func (d *DB) CreateAPIToken(email, name string, scopes []string, ttlDays int, ip string) (full string, meta *APIToken, err error) {
	idBytes := make([]byte, 6)
	if _, err = rand.Read(idBytes); err != nil {
		return "", nil, err
	}
	keyID := hex.EncodeToString(idBytes) // 12 hex chars

	secBytes := make([]byte, 32)
	if _, err = rand.Read(secBytes); err != nil {
		return "", nil, err
	}
	secret := base64.RawURLEncoding.EncodeToString(secBytes)

	full = TokenPrefix + keyID + "_" + secret

	var expires any
	if ttlDays > 0 {
		expires = time.Now().UTC().AddDate(0, 0, ttlDays).Format("2006-01-02 15:04:05")
	}

	for _, s := range scopes {
		if !IsValidScope(s) {
			return "", nil, fmt.Errorf("invalid scope: %s", s)
		}
	}
	if len(scopes) == 0 {
		scopes = []string{ScopeJobsRead}
	}

	_, err = d.conn.Exec(`
		INSERT INTO api_tokens (key_id, user_email, token_hash, name, scopes, expires_at, created_ip)
		VALUES (?, ?, ?, ?, ?, ?, ?)
	`, keyID, strings.ToLower(email), hashToken(secret), name, strings.Join(scopes, ","), expires, ip)
	if err != nil {
		return "", nil, err
	}
	return full, &APIToken{
		KeyID:     keyID,
		UserEmail: strings.ToLower(email),
		Name:      name,
		Scopes:    scopes,
		Display:   TokenPrefix + keyID + "_…",
	}, nil
}

// LookupAPIToken resolves a presented token string to its owner.
//
// It returns ErrTokenNotFound for anything malformed, unknown or mismatched —
// deliberately the same error for all three, so a caller cannot distinguish
// "no such key id" from "wrong secret".
func (d *DB) LookupAPIToken(presented string) (*APIToken, error) {
	if !strings.HasPrefix(presented, TokenPrefix) {
		return nil, ErrTokenNotFound
	}
	rest := strings.TrimPrefix(presented, TokenPrefix)
	parts := strings.SplitN(rest, "_", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return nil, ErrTokenNotFound
	}
	keyID, secret := parts[0], parts[1]

	var (
		email, storedHash, name, scopes, createdAt string
		lastUsed, expiresAt, revokedAt             sql.NullString
	)
	err := d.conn.QueryRow(`
		SELECT user_email, token_hash, name, scopes, created_at, last_used_at, expires_at, revoked_at
		FROM api_tokens WHERE key_id = ?
	`, keyID).Scan(&email, &storedHash, &name, &scopes, &createdAt, &lastUsed, &expiresAt, &revokedAt)
	if err == sql.ErrNoRows {
		return nil, ErrTokenNotFound
	}
	if err != nil {
		return nil, err
	}

	// Constant-time compare so response timing does not leak how much of the
	// hash matched.
	if subtle.ConstantTimeCompare([]byte(hashToken(secret)), []byte(storedHash)) != 1 {
		return nil, ErrTokenNotFound
	}
	if revokedAt.Valid && revokedAt.String != "" {
		return nil, ErrTokenRevoked
	}
	if expiresAt.Valid && expiresAt.String != "" {
		exp, perr := parseSQLiteTime(expiresAt.String)
		if perr != nil {
			// Fail closed: an expiry we cannot read is treated as expired,
			// never as "no expiry".
			return nil, ErrTokenExpired
		}
		if time.Now().UTC().After(exp) {
			return nil, ErrTokenExpired
		}
	}

	return &APIToken{
		KeyID:       keyID,
		UserEmail:   email,
		Name:        name,
		Scopes:      strings.Split(scopes, ","),
		CreatedAt:   createdAt,
		LastUsedAt:  nullStringPtr(lastUsed),
		ExpiresAt:   nullStringPtr(expiresAt),
		RevokedAt:   nullStringPtr(revokedAt),
		Display:     TokenPrefix + keyID + "_…",
	}, nil
}

// nullStringPtr returns a pointer to the string when valid and non-empty, nil otherwise.
func nullStringPtr(s sql.NullString) *string {
	if !s.Valid || s.String == "" {
		return nil
	}
	return &s.String
}

// ListAPITokens returns all tokens for a user, newest first, including revoked
// ones. It must never select token_hash.
func (d *DB) ListAPITokens(email string) ([]APIToken, error) {
	rows, err := d.conn.Query(`
		SELECT key_id, name, scopes, created_at, last_used_at, expires_at, revoked_at
		FROM api_tokens WHERE user_email = ?
		ORDER BY created_at DESC
	`, strings.ToLower(email))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var tokens []APIToken
	for rows.Next() {
		var t APIToken
		var scopes string
		var lastUsed, expiresAt, revokedAt sql.NullString
		if err := rows.Scan(&t.KeyID, &t.Name, &scopes, &t.CreatedAt, &lastUsed, &expiresAt, &revokedAt); err != nil {
			return nil, err
		}
		t.UserEmail = email
		t.Scopes = strings.Split(scopes, ",")
		t.LastUsedAt = nullStringPtr(lastUsed)
		t.ExpiresAt = nullStringPtr(expiresAt)
		t.RevokedAt = nullStringPtr(revokedAt)
		t.Display = TokenPrefix + t.KeyID + "_…"
		tokens = append(tokens, t)
	}
	if tokens == nil {
		tokens = []APIToken{}
	}
	return tokens, nil
}

// RevokeAPIToken soft-deletes a token by setting revoked_at. It filters on
// both key_id and user_email so one user cannot revoke another's token by
// guessing a key id.
func (d *DB) RevokeAPIToken(keyID, email string) error {
	result, err := d.conn.Exec(`
		UPDATE api_tokens SET revoked_at = datetime('now')
		WHERE key_id = ? AND user_email = ? AND revoked_at IS NULL
	`, keyID, strings.ToLower(email))
	if err != nil {
		return err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

// TouchAPITokenLastUsed updates last_used_at for a key id. Callers should
// throttle writes (at most once per minute per key id) and perform the write
// off the request path; see TokenMiddleware.
func (d *DB) TouchAPITokenLastUsed(keyID string) error {
	_, err := d.conn.Exec(`UPDATE api_tokens SET last_used_at = datetime('now') WHERE key_id = ?`, keyID)
	return err
}

func parseSQLiteTime(s string) (time.Time, error) {
	for _, layout := range []string{
		"2006-01-02 15:04:05",
		"2006-01-02T15:04:05Z07:00",
		time.RFC3339,
		"2006-01-02 15:04:05.999999999 -0700 MST",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC(), nil
		}
	}
	return time.Time{}, fmt.Errorf("unrecognised timestamp: %q", s)
}
