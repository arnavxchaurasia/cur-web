package db_test

import (
	"testing"
	"time"

	"github.com/facets/cur-web/internal/db"
	"github.com/stretchr/testify/require"
	"github.com/stretchr/testify/assert"
)

// An expiry the parser cannot read must make the share EXPIRED, never
// permanent. Share links are served from the public, unauthenticated
// /api/reports/{token} route, and a nil ExpiresAt there means "never expires".
func TestReportShare_UnparseableExpiryIsTreatedAsExpired(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	require.NoError(t, d.CreateReportShare("tok-bad", "job-1", "a@facets.cloud", time.Now().Add(24*time.Hour)))
	_, err = d.ExecForTest(`UPDATE report_shares SET expires_at = 'not-a-timestamp' WHERE token = ?`, "tok-bad")
	require.NoError(t, err)

	share, err := d.GetReportShare("tok-bad")
	require.NoError(t, err)
	require.NotNil(t, share)
	require.NotNil(t, share.ExpiresAt, "an unreadable expiry must not become nil (nil means never expires)")
	assert.True(t, time.Now().After(*share.ExpiresAt), "an unreadable expiry must read as already expired")
}

// A well-formed future expiry still works.
func TestReportShare_ValidExpiryIsPreserved(t *testing.T) {
	d, err := db.Open(":memory:")
	require.NoError(t, err)
	defer d.Close()

	future := time.Now().Add(48 * time.Hour)
	require.NoError(t, d.CreateReportShare("tok-ok", "job-1", "a@facets.cloud", future))

	share, err := d.GetReportShare("tok-ok")
	require.NoError(t, err)
	require.NotNil(t, share)
	require.NotNil(t, share.ExpiresAt)
	assert.True(t, share.ExpiresAt.After(time.Now()), "a valid future expiry must stay in the future")
}
