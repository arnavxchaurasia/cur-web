package db_test

import (
	"database/sql"
	"strings"
	"testing"

	"github.com/facets/cur-web/internal/db"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newAPITokenDB(t *testing.T) *db.DB {
	d := newTestDB(t)
	t.Cleanup(func() { d.Close() })
	return d
}

func splitToken(full string) (keyID, secret string, ok bool) {
	if !strings.HasPrefix(full, db.TokenPrefix) {
		return "", "", false
	}
	rest := strings.TrimPrefix(full, db.TokenPrefix)
	parts := strings.SplitN(rest, "_", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

// 1. Create → the returned string starts with fct_live_, parses into key id +
// secret, and LookupAPIToken resolves it to the right email.
func TestCreateAPITokenResolvesToOwner(t *testing.T) {
	d := newAPITokenDB(t)
	email := "alice@example.com"

	full, meta, err := d.CreateAPIToken(email, "Praxis laptop", []string{db.ScopeJobsRead}, 90, "127.0.0.1")
	require.NoError(t, err)
	assert.True(t, strings.HasPrefix(full, db.TokenPrefix))

	keyID, _, ok := splitToken(full)
	assert.True(t, ok, "token must parse into key id + secret")
	assert.Equal(t, 12, len(keyID), "key id must be 12 hex chars")
	assert.Equal(t, meta.KeyID, keyID)
	assert.Equal(t, meta.UserEmail, email)
	assert.Equal(t, meta.Display, db.TokenPrefix+keyID+"_…")

	got, err := d.LookupAPIToken(full)
	require.NoError(t, err)
	assert.Equal(t, email, got.UserEmail)
	assert.Equal(t, keyID, got.KeyID)
	assert.Equal(t, "Praxis laptop", got.Name)
	assert.Equal(t, []string{db.ScopeJobsRead}, got.Scopes)
}

// 2. Two different users get different tokens, each resolving to its own owner
// (the reported "same token for different users" symptom, pinned).
func TestDifferentUsersGetDifferentTokens(t *testing.T) {
	d := newAPITokenDB(t)

	fullA, metaA, err := d.CreateAPIToken("a@example.com", "A", nil, 0, "1.1.1.1")
	require.NoError(t, err)
	fullB, metaB, err := d.CreateAPIToken("b@example.com", "B", nil, 0, "2.2.2.2")
	require.NoError(t, err)

	assert.NotEqual(t, fullA, fullB)
	assert.NotEqual(t, metaA.KeyID, metaB.KeyID)

	gotA, err := d.LookupAPIToken(fullA)
	require.NoError(t, err)
	assert.Equal(t, "a@example.com", gotA.UserEmail)
	gotB, err := d.LookupAPIToken(fullB)
	require.NoError(t, err)
	assert.Equal(t, "b@example.com", gotB.UserEmail)
}

// 3. Two tokens for the same user are different, and revoking one leaves the
// other working.
func TestSameUserTwoTokensRevokeOne(t *testing.T) {
	d := newAPITokenDB(t)
	email := "carol@example.com"

	full1, meta1, err := d.CreateAPIToken(email, "one", nil, 0, "1.1.1.1")
	require.NoError(t, err)
	full2, meta2, err := d.CreateAPIToken(email, "two", nil, 0, "2.2.2.2")
	require.NoError(t, err)
	assert.NotEqual(t, full1, full2)
	assert.NotEqual(t, meta1.KeyID, meta2.KeyID)

	require.NoError(t, d.RevokeAPIToken(meta1.KeyID, email))

	// Revoked token no longer resolves.
	_, err = d.LookupAPIToken(full1)
	assert.ErrorIs(t, err, db.ErrTokenRevoked)

	// The other token still works.
	got, err := d.LookupAPIToken(full2)
	require.NoError(t, err)
	assert.Equal(t, email, got.UserEmail)
	assert.Equal(t, "two", got.Name)
}

// 4. The database never holds the secret: token_hash is not the returned token
// and the full token string appears nowhere in the table.
func TestSecretNotStored(t *testing.T) {
	d := newAPITokenDB(t)
	email := "dave@example.com"

	full, meta, err := d.CreateAPIToken(email, "x", nil, 0, "1.1.1.1")
	require.NoError(t, err)

	var tokenHash string
	require.NoError(t, d.QueryRowForTest(`SELECT token_hash FROM api_tokens WHERE key_id = ?`, meta.KeyID).Scan(&tokenHash))
	assert.NotEqual(t, full, tokenHash, "token_hash must not equal the full token")

	// The full token string must not appear anywhere in the table.
	var count int
	require.NoError(t, d.QueryRowForTest(`SELECT COUNT(*) FROM api_tokens WHERE token_hash = ? OR key_id = ?`, full, full).Scan(&count))
	assert.Equal(t, 0, count)
}

// 5. A tampered secret with a valid key id → ErrTokenNotFound.
func TestTamperedSecretNotFound(t *testing.T) {
	d := newAPITokenDB(t)
	full, _, err := d.CreateAPIToken("eve@example.com", "x", nil, 0, "1.1.1.1")
	require.NoError(t, err)

	keyID, _, _ := splitToken(full)
	tampered := db.TokenPrefix + keyID + "_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ" // wrong secret
	_, err = d.LookupAPIToken(tampered)
	assert.ErrorIs(t, err, db.ErrTokenNotFound)
}

// 6. Malformed strings → ErrTokenNotFound, no panic.
func TestMalformedTokensNotFound(t *testing.T) {
	d := newAPITokenDB(t)
	for _, bad := range []string{
		"",
		"no_prefix_at_all",
		db.TokenPrefix,                          // prefix only
		db.TokenPrefix + "nosecret",             // no underscore
		db.TokenPrefix + "keyid_",               // empty secret
		db.TokenPrefix + "_secret",              // empty key id
	} {
		_, err := d.LookupAPIToken(bad)
		assert.ErrorIs(t, err, db.ErrTokenNotFound, "expected ErrTokenNotFound for %q", bad)
	}
}

// 7. Revoked → ErrTokenRevoked. Expired → ErrTokenExpired.
func TestRevokedAndExpired(t *testing.T) {
	d := newAPITokenDB(t)

	fullRevoked, metaRevoked, err := d.CreateAPIToken("revoked@example.com", "r", nil, 0, "1.1.1.1")
	require.NoError(t, err)
	require.NoError(t, d.RevokeAPIToken(metaRevoked.KeyID, "revoked@example.com"))
	_, err = d.LookupAPIToken(fullRevoked)
	assert.ErrorIs(t, err, db.ErrTokenRevoked)

	// Expired: force expires_at into the past directly.
	fullExpired, metaExpired, err := d.CreateAPIToken("expired@example.com", "e", nil, 0, "1.1.1.1")
	require.NoError(t, err)
	_, err = d.ExecForTest(`UPDATE api_tokens SET expires_at = '2000-01-01 00:00:00' WHERE key_id = ?`, metaExpired.KeyID)
	require.NoError(t, err)
	_, err = d.LookupAPIToken(fullExpired)
	assert.ErrorIs(t, err, db.ErrTokenExpired)

	// Unparseable expires_at: fail closed → ErrTokenExpired
	fullBadTime, metaBadTime, err := d.CreateAPIToken("badtime@example.com", "bt", nil, 0, "1.1.1.1")
	require.NoError(t, err)
	_, err = d.ExecForTest(`UPDATE api_tokens SET expires_at = 'not-a-valid-timestamp' WHERE key_id = ?`, metaBadTime.KeyID)
	require.NoError(t, err)
	_, err = d.LookupAPIToken(fullBadTime)
	assert.ErrorIs(t, err, db.ErrTokenExpired)
}

// 8. User A cannot revoke user B's token — RevokeAPIToken(bKeyID, aEmail)
// leaves B's token working.
func TestCannotRevokeOthersToken(t *testing.T) {
	d := newAPITokenDB(t)

	fullB, metaB, err := d.CreateAPIToken("bob@example.com", "bob-token", nil, 0, "1.1.1.1")
	require.NoError(t, err)

	err = d.RevokeAPIToken(metaB.KeyID, "alice@example.com")
	assert.ErrorIs(t, err, sql.ErrNoRows, "alice must not be able to revoke bob's token")

	// Bob's token still works after Alice's failed revoke attempt.
	got, err := d.LookupAPIToken(fullB)
	require.NoError(t, err)
	assert.Equal(t, "bob@example.com", got.UserEmail)
	assert.Nil(t, got.RevokedAt)
}
