package db_test

import (
	"path/filepath"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// 1. UpsertUserSeen(email, "admin") creates the row with role "admin",
// and a second call with "viewer" does NOT change the existing row.
func TestUpsertUserSeenNeverOverwritesExistingRole(t *testing.T) {
	d := newTestDB(t)
	email := "alice@facets.cloud"

	err := d.UpsertUserSeen(email, "admin")
	require.NoError(t, err)

	role, err := d.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "admin", role)

	// Second upsert with "viewer" must not touch the stored role.
	err = d.UpsertUserSeen(email, "viewer")
	require.NoError(t, err)

	role, err = d.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "admin", role, "UpsertUserSeen must never overwrite an existing row's role")
}

// 2. A Facets account is still admin on its second login.
// Drives the exact path the auth handler uses: resolve role, upsert, resolve again.
func TestFacetsAccountStillAdminOnSecondLogin(t *testing.T) {
	d := newTestDB(t)
	email := "bob@facets.cloud"

	// Login 1:
	isFacets := auth.IsFacetsEmail(email)
	role1 := "viewer"
	if isFacets {
		role1 = "admin"
	}
	r1, err := d.GetUserRole(email)
	require.NoError(t, err)
	if r1 != "" {
		role1 = r1
	}
	err = d.UpsertUserSeen(email, role1)
	require.NoError(t, err)

	sess1 := &auth.Session{Email: email, Role: role1}
	assert.True(t, sess1.IsAdmin(nil), "Facets account must be admin on 1st login")

	// Login 2:
	role2 := "viewer"
	if isFacets {
		role2 = "admin"
	}
	r2, err := d.GetUserRole(email)
	require.NoError(t, err)
	if r2 != "" {
		role2 = r2
	}
	err = d.UpsertUserSeen(email, role2)
	require.NoError(t, err)

	sess2 := &auth.Session{Email: email, Role: role2}
	assert.True(t, sess2.IsAdmin(nil), "Facets account must still be admin on 2nd login")
}

// 3. An address in ADMIN_EMAILS whose stored role is "viewer" resolves as admin (break-glass).
func TestAdminEmailsOverridesStoredViewerRole(t *testing.T) {
	d := newTestDB(t)
	email := "emergency@facets.cloud"

	err := d.SetUserRole(email, "viewer")
	require.NoError(t, err)

	role, err := d.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "viewer", role)

	sess := &auth.Session{Email: email, Role: role}
	adminEmails := []string{"emergency@facets.cloud"}
	assert.True(t, sess.IsAdmin(adminEmails), "ADMIN_EMAILS must act as break-glass even when stored role is viewer")
}

// 4. A non-Facets address can be granted admin and revoked back to viewer.
func TestNonFacetsAdminGrantAndRevoke(t *testing.T) {
	d := newTestDB(t)
	email := "external-partner@gmail.com"

	err := d.SetUserRole(email, "admin")
	require.NoError(t, err)

	role, err := d.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "admin", role)

	sess := &auth.Session{Email: email, Role: role, AdminGrantedBy: "boss@facets.cloud"}
	assert.True(t, sess.IsAdmin(nil), "Non-Facets address granted admin role must be admin")

	// Revoke admin access
	err = d.SetUserRole(email, "viewer")
	require.NoError(t, err)
	roleRevoked, err := d.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "viewer", roleRevoked)

	sessRevoked := &auth.Session{Email: email, Role: roleRevoked}
	assert.False(t, sessRevoked.IsAdmin(nil), "Non-Facets address revoked to viewer must not be admin")

	// Break-glass adminEmails grants admin even if viewer
	assert.True(t, sessRevoked.IsAdmin([]string{"external-partner@gmail.com"}), "Non-facets address in adminEmails must be admin")
}

// 5. Test that demotion of a Facets user survives restarts (A-3 one-shot schema migration).
func TestFacetsUserDemotionSurvivesDbRestarts(t *testing.T) {
	tmpDir := t.TempDir()
	dbPath := filepath.Join(tmpDir, "test_restart.db")

	// 1st start
	d1, err := db.Open(dbPath)
	require.NoError(t, err)

	email := "charlie@facets.cloud"
	require.NoError(t, d1.UpsertUserSeen(email, "admin"))

	// Demote user to viewer
	require.NoError(t, d1.SetUserRole(email, "viewer"))
	r, err := d1.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "viewer", r)
	d1.Close()

	// 2nd start (restart)
	d2, err := db.Open(dbPath)
	require.NoError(t, err)
	r2, err := d2.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "viewer", r2, "Demoted role must survive 1st restart")
	d2.Close()

	// 3rd start (second restart)
	d3, err := db.Open(dbPath)
	require.NoError(t, err)
	r3, err := d3.GetUserRole(email)
	require.NoError(t, err)
	assert.Equal(t, "viewer", r3, "Demoted role must survive 2nd restart")
	d3.Close()
}

// 6. merge URL-encoded ghost row when no genuine row exists → renamed with role intact.
func TestMergeURLEncodedGhostNoGenuineRow(t *testing.T) {
	d := newTestDB(t)
	ghost := "arnavxchaurasia%40gmail.com"
	_, execErr1 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'admin', 'grantor@facets.cloud', datetime('now'), datetime('now'), datetime('now'))`, ghost)
	require.NoError(t, execErr1)

	require.NoError(t, db.MergeURLEncodedUserRows(d.Conn()))

	role, grantedBy, err := d.GetUserRoleAndGrant("arnavxchaurasia@gmail.com")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)
	assert.Equal(t, "grantor@facets.cloud", grantedBy)

	// No row containing '%' remains.
	rows, err := d.ExecForTest(`SELECT COUNT(*) FROM users WHERE email LIKE '%\%%' ESCAPE '\'`)
	require.NoError(t, err)
	count, _ := rows.RowsAffected()
	assert.EqualValues(t, 0, count)
}

// 7. ghost admin + genuine viewer → one row, admin, grant metadata preserved.
func TestMergeURLEncodedGhostAdminOverridesViewer(t *testing.T) {
	d := newTestDB(t)
	_, execErr2 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'viewer', NULL, NULL, datetime('now'), datetime('now'))`, "arnav.chaurasia@facets.cloud")
	require.NoError(t, execErr2)
	_, execErr3 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'admin', 'grantor@facets.cloud', datetime('now'), datetime('now'), datetime('now'))`, "arnav.chaurasia%40facets.cloud")
	require.NoError(t, execErr3)

	require.NoError(t, db.MergeURLEncodedUserRows(d.Conn()))

	role, grantedBy, err := d.GetUserRoleAndGrant("arnav.chaurasia@facets.cloud")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)
	assert.Equal(t, "grantor@facets.cloud", grantedBy)
}

// 8. ghost viewer + genuine admin → one row, still admin (no downgrade).
func TestMergeURLEncodedGhostViewerKeepsAdmin(t *testing.T) {
	d := newTestDB(t)
	_, execErr4 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'admin', 'grantor@facets.cloud', datetime('now'), datetime('now'), datetime('now'))`, "arnav.chaurasia@facets.cloud")
	require.NoError(t, execErr4)
	_, execErr5 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'viewer', NULL, NULL, datetime('now'), datetime('now'))`, "arnav.chaurasia%40facets.cloud")
	require.NoError(t, execErr5)

	require.NoError(t, db.MergeURLEncodedUserRows(d.Conn()))

	role, _, err := d.GetUserRoleAndGrant("arnav.chaurasia@facets.cloud")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)
}

// 9. running the migration twice changes nothing the second time.
func TestMergeURLEncodedIdempotent(t *testing.T) {
	d := newTestDB(t)
	_, execErr6 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'admin', 'grantor@facets.cloud', datetime('now'), datetime('now'), datetime('now'))`, "arnavxchaurasia%40gmail.com")
	require.NoError(t, execErr6)

	require.NoError(t, db.MergeURLEncodedUserRows(d.Conn()))
	require.NoError(t, db.MergeURLEncodedUserRows(d.Conn()))

	role, _, err := d.GetUserRoleAndGrant("arnavxchaurasia@gmail.com")
	require.NoError(t, err)
	assert.Equal(t, "admin", role)

	rows, err := d.ExecForTest(`SELECT COUNT(*) FROM users WHERE email LIKE '%\%%' ESCAPE '\'`)
	require.NoError(t, err)
	count, _ := rows.RowsAffected()
	assert.EqualValues(t, 0, count)
}

// 10. a row containing '%' that does not decode cleanly is left untouched.
func TestMergeURLEncodedBadDecodeLeftAlone(t *testing.T) {
	d := newTestDB(t)
	bad := "not-an-email%"
	_, execErr7 := d.ExecForTest(`INSERT INTO users (email, role, admin_granted_by, admin_granted_at, created_at, updated_at) VALUES (?, 'viewer', NULL, NULL, datetime('now'), datetime('now'))`, bad)
	require.NoError(t, execErr7)

	require.NoError(t, db.MergeURLEncodedUserRows(d.Conn()))

	role, err := d.GetUserRole(bad)
	require.NoError(t, err)
	assert.Equal(t, "viewer", role)
}
