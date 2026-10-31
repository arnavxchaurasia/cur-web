// Package db — integration_settings layer: admin-configurable notification
// settings (email + Slack) stored in the database so operators configure them
// in-app at deploy time instead of only via env vars. Secret values (API keys,
// webhook URLs) are encrypted at rest with AES-256-GCM; they are never returned
// to the client and never logged.
package db

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

// ErrNoEncryptionKey is returned when a secret must be stored but
// SETTINGS_ENCRYPTION_KEY is unset/invalid. We refuse to store secrets in
// plaintext rather than silently weaken security.
var ErrNoEncryptionKey = errors.New("SETTINGS_ENCRYPTION_KEY not set or invalid (need base64 of 32 bytes); cannot store secret")

// ErrSettingNotFound is returned when a key has no row.
var ErrSettingNotFound = errors.New("integration setting not found")

// IntegrationSetting is the masked, client-safe view of one setting. For
// secrets, Value is never populated — only Configured/Last4.
type IntegrationSetting struct {
	Key        string `json:"key"`
	Value      string `json:"value,omitempty"`
	IsSecret   bool   `json:"is_secret"`
	Configured bool   `json:"configured"`
	Last4      string `json:"last4,omitempty"`
	UpdatedBy  string `json:"updated_by,omitempty"`
	UpdatedAt  string `json:"updated_at,omitempty"`
}

// encryptionKey reads and validates the 32-byte AES key from the environment.
func encryptionKey() ([]byte, error) {
	raw := strings.TrimSpace(os.Getenv("SETTINGS_ENCRYPTION_KEY"))
	if raw == "" {
		return nil, ErrNoEncryptionKey
	}
	key, err := base64.StdEncoding.DecodeString(raw)
	if err != nil || len(key) != 32 {
		return nil, ErrNoEncryptionKey
	}
	return key, nil
}

// EncryptionAvailable reports whether a valid SETTINGS_ENCRYPTION_KEY is set,
// so the admin UI can warn before a secret save fails.
func EncryptionAvailable() bool {
	_, err := encryptionKey()
	return err == nil
}

func encryptSecret(plaintext string) ([]byte, error) {
	key, err := encryptionKey()
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}
	// Prepend nonce to ciphertext so decrypt is self-contained.
	return gcm.Seal(nonce, nonce, []byte(plaintext), nil), nil
}

func decryptSecret(blob []byte) (string, error) {
	key, err := encryptionKey()
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	ns := gcm.NonceSize()
	if len(blob) < ns {
		return "", fmt.Errorf("ciphertext too short")
	}
	nonce, ct := blob[:ns], blob[ns:]
	pt, err := gcm.Open(nil, nonce, ct, nil)
	if err != nil {
		return "", err
	}
	return string(pt), nil
}

// SetIntegrationSetting upserts a setting. For secrets the value is encrypted;
// an empty value for a secret is treated as "leave unchanged" by the caller
// (handlers skip the call), so this always writes what it is given.
func (d *DB) SetIntegrationSetting(key, value string, isSecret bool, updatedBy string) error {
	if isSecret {
		enc, err := encryptSecret(value)
		if err != nil {
			return err
		}
		_, err = d.conn.Exec(`
			INSERT INTO integration_settings (key, value, secret_enc, is_secret, updated_by, updated_at)
			VALUES (?, NULL, ?, 1, ?, datetime('now'))
			ON CONFLICT(key) DO UPDATE SET value=NULL, secret_enc=excluded.secret_enc, is_secret=1, updated_by=excluded.updated_by, updated_at=datetime('now')
		`, key, enc, updatedBy)
		return err
	}
	_, err := d.conn.Exec(`
		INSERT INTO integration_settings (key, value, secret_enc, is_secret, updated_by, updated_at)
		VALUES (?, ?, NULL, 0, ?, datetime('now'))
		ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret_enc=NULL, is_secret=0, updated_by=excluded.updated_by, updated_at=datetime('now')
	`, key, value, updatedBy)
	return err
}

// GetIntegrationValue returns the plaintext value for a non-secret key, or the
// decrypted value for a secret key. Returns ErrSettingNotFound if absent. Use
// this server-side only — never send a secret's result to a client.
func (d *DB) GetIntegrationValue(key string) (string, error) {
	var value sql.NullString
	var secretEnc []byte
	var isSecret int
	err := d.conn.QueryRow(`SELECT value, secret_enc, is_secret FROM integration_settings WHERE key = ?`, key).Scan(&value, &secretEnc, &isSecret)
	if err == sql.ErrNoRows {
		return "", ErrSettingNotFound
	}
	if err != nil {
		return "", err
	}
	if isSecret == 1 {
		if len(secretEnc) == 0 {
			return "", nil
		}
		return decryptSecret(secretEnc)
	}
	return value.String, nil
}

// GetIntegrationValueOr returns the resolved value, or fallback if the key is
// absent or cannot be decrypted. This is the resolver primitive: DB value wins,
// else the env fallback.
func (d *DB) GetIntegrationValueOr(key, fallback string) string {
	v, err := d.GetIntegrationValue(key)
	if err != nil || v == "" {
		return fallback
	}
	return v
}

// ListIntegrationSettings returns all settings in masked form: secret values
// are reduced to configured/last4 and never leave the server in the clear.
func (d *DB) ListIntegrationSettings() (map[string]IntegrationSetting, error) {
	rows, err := d.conn.Query(`SELECT key, value, secret_enc, is_secret, updated_by, updated_at FROM integration_settings`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]IntegrationSetting{}
	for rows.Next() {
		var key, updatedBy, updatedAt string
		var value sql.NullString
		var secretEnc []byte
		var isSecret int
		if err := rows.Scan(&key, &value, &secretEnc, &isSecret, &updatedBy, &updatedAt); err != nil {
			return nil, err
		}
		s := IntegrationSetting{Key: key, IsSecret: isSecret == 1, UpdatedBy: updatedBy, UpdatedAt: updatedAt}
		if isSecret == 1 {
			s.Configured = len(secretEnc) > 0
			if s.Configured {
				if pt, derr := decryptSecret(secretEnc); derr == nil && len(pt) >= 4 {
					s.Last4 = pt[len(pt)-4:]
				}
			}
		} else {
			s.Value = value.String
			s.Configured = value.String != ""
		}
		out[key] = s
	}
	return out, rows.Err()
}

// DeleteIntegrationSetting removes a key (reverts to env/default).
func (d *DB) DeleteIntegrationSetting(key string) error {
	_, err := d.conn.Exec(`DELETE FROM integration_settings WHERE key = ?`, key)
	return err
}
