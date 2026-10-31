// internal/db/users.go
package db

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"strings"
)

// isFacetsEmail reports whether email belongs to a Facets-owned domain. It is
// duplicated here (canonical: internal/auth.IsFacetsEmail) so that db does not
// import auth, which would create an import cycle via the auth token middleware
// importing db. Keep the two domain lists in sync.
func isFacetsEmail(email string) bool {
	lower := strings.ToLower(strings.TrimSpace(email))
	at := strings.LastIndex(lower, "@")
	if at < 0 || at == len(lower)-1 {
		return false
	}
	domain := lower[at+1:]
	return domain == "facets.cloud" || domain == "facets.io"
}

func hashPassword(password string) string {
	salt := make([]byte, 16)
	_, _ = rand.Read(salt)
	h := sha256.Sum256(append(salt, []byte(password)...))
	return hex.EncodeToString(salt) + ":" + hex.EncodeToString(h[:])
}

func verifyPassword(password, stored string) bool {
	parts := strings.Split(stored, ":")
	if len(parts) != 2 {
		return false
	}
	salt, err := hex.DecodeString(parts[0])
	if err != nil {
		return false
	}
	expectedHash, err := hex.DecodeString(parts[1])
	if err != nil {
		return false
	}
	actualHash := sha256.Sum256(append(salt, []byte(password)...))
	return subtle.ConstantTimeCompare(actualHash[:], expectedHash) == 1
}

// AuthenticateUser verifies or creates credentials for email.
// If the user does not exist, it registers them with the given password.
// If the user exists without password, it associates this password.
// If the user exists with password, it checks password matching.
func (d *DB) AuthenticateUser(email, password string) (bool, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	if email == "" || password == "" {
		return false, nil
	}

	var storedHash sql.NullString
	var role string
	err := d.conn.QueryRow(`SELECT password_hash, role FROM users WHERE email = ?`, email).Scan(&storedHash, &role)
	if err == sql.ErrNoRows {
		isFacets := isFacetsEmail(email)
		newRole := "viewer"
		if isFacets {
			newRole = "admin"
		}
		newHash := hashPassword(password)
		_, err := d.conn.Exec(`INSERT INTO users (email, role, password_hash) VALUES (?, ?, ?)`, email, newRole, newHash)
		if err != nil {
			return false, err
		}
		return true, nil
	}
	if err != nil {
		return false, err
	}

	if !storedHash.Valid || storedHash.String == "" {
		newHash := hashPassword(password)
		_, err := d.conn.Exec(`UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE email = ?`, newHash, email)
		if err != nil {
			return false, err
		}
		return true, nil
	}

	if !verifyPassword(password, storedHash.String) {
		return false, nil
	}

	_, _ = d.conn.Exec(`UPDATE users SET updated_at = datetime('now') WHERE email = ?`, email)
	return true, nil
}

// UpsertUserSeen records that email successfully logged in, creating a
// row with role on first sight and bumping updated_at on every
// subsequent login. Never touches an existing row's role — promotion/
// demotion only happens via SetUserRole.
func (d *DB) UpsertUserSeen(email, role string) error {
	_, err := d.conn.Exec(
		`INSERT INTO users (email, role) VALUES (?, ?)
		 ON CONFLICT(email) DO UPDATE SET updated_at=datetime('now')`,
		email, role,
	)
	return err
}

// UserRoleInfo holds role and granter metadata for session resolution.
type UserRoleInfo struct {
	Role           string
	AdminGrantedBy string
}

// GetUserRole returns the role stored for email, or ("", nil) if no row
// exists yet (caller should fall back to the admin-email-list bootstrap).
func (d *DB) GetUserRole(email string) (string, error) {
	info, err := d.GetUserRoleInfo(email)
	if err != nil {
		return "", err
	}
	return info.Role, nil
}

// GetUserRoleInfo returns both the role and admin_granted_by stored for email.
func (d *DB) GetUserRoleInfo(email string) (UserRoleInfo, error) {
	var role string
	var grantedBy sql.NullString
	err := d.conn.QueryRow(`SELECT role, admin_granted_by FROM users WHERE email = ?`, email).Scan(&role, &grantedBy)
	if err == sql.ErrNoRows {
		return UserRoleInfo{}, nil
	}
	if err != nil {
		return UserRoleInfo{}, err
	}
	return UserRoleInfo{
		Role:           role,
		AdminGrantedBy: grantedBy.String,
	}, nil
}

// GetUserRoleAndGrant returns role and admin_granted_by stored for email.
func (d *DB) GetUserRoleAndGrant(email string) (string, string, error) {
	info, err := d.GetUserRoleInfo(email)
	if err != nil {
		return "", "", err
	}
	return info.Role, info.AdminGrantedBy, nil
}

// SetUserRole creates or updates the role for email.
func (d *DB) SetUserRole(email, role string) error {
	return d.SetUserRoleWithGrant(email, role, "")
}

// SetUserRoleWithGrant creates or updates the role for email.
// When granting admin to a non-facets user, granterEmail is recorded.
// When revoking admin or changing to non-admin, admin_granted_by and admin_granted_at are cleared.
func (d *DB) SetUserRoleWithGrant(email, role, granterEmail string) error {
	if role == "admin" && granterEmail != "" {
		_, err := d.conn.Exec(`
			INSERT INTO users (email, role, admin_granted_by, admin_granted_at) VALUES (?, ?, ?, datetime('now'))
			ON CONFLICT(email) DO UPDATE SET role=excluded.role, admin_granted_by=excluded.admin_granted_by, admin_granted_at=datetime('now'), updated_at=datetime('now')
		`, email, role, granterEmail)
		return err
	}
	_, err := d.conn.Exec(`
		INSERT INTO users (email, role, admin_granted_by, admin_granted_at) VALUES (?, ?, NULL, NULL)
		ON CONFLICT(email) DO UPDATE SET role=excluded.role, admin_granted_by=NULL, admin_granted_at=NULL, updated_at=datetime('now')
	`, email, role)
	return err
}

type User struct {
	Email          string  `json:"email"`
	Role           string  `json:"role"`
	AdminGrantedBy *string `json:"admin_granted_by,omitempty"`
	AdminGrantedAt *string `json:"admin_granted_at,omitempty"`
	CreatedAt      string  `json:"created_at"`
	UpdatedAt      string  `json:"updated_at"`
}

// ListUsers returns every known user, newest-seen first.
func (d *DB) ListUsers() ([]*User, error) {
	rows, err := d.conn.Query(`SELECT email, role, admin_granted_by, admin_granted_at, created_at, updated_at FROM users ORDER BY updated_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var users []*User
	for rows.Next() {
		var u User
		var grantedBy, grantedAt sql.NullString
		if err := rows.Scan(&u.Email, &u.Role, &grantedBy, &grantedAt, &u.CreatedAt, &u.UpdatedAt); err != nil {
			return nil, err
		}
		if grantedBy.Valid && grantedBy.String != "" {
			u.AdminGrantedBy = &grantedBy.String
		}
		if grantedAt.Valid && grantedAt.String != "" {
			u.AdminGrantedAt = &grantedAt.String
		}
		users = append(users, &u)
	}
	return users, nil
}

// generateToken returns a secure random bearer token prefix with fct_live_
func generateToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "fct_live_" + hex.EncodeToString(b)
}

// GetOrCreateAPIToken returns the persistent API token for a user, generating one if not present.
func (d *DB) GetOrCreateAPIToken(email string) (string, error) {
	var token sql.NullString
	err := d.conn.QueryRow(`SELECT api_token FROM users WHERE email = ?`, email).Scan(&token)
	if err == nil && token.Valid && token.String != "" {
		return token.String, nil
	}
	newToken := generateToken()
	_, err = d.conn.Exec(`
		INSERT INTO users (email, api_token, updated_at) VALUES (?, ?, datetime('now'))
		ON CONFLICT(email) DO UPDATE SET api_token = excluded.api_token, updated_at = datetime('now')
	`, email, newToken)
	if err != nil {
		return "", err
	}
	return newToken, nil
}

// RegenerateAPIToken generates and returns a new API token for a user.
func (d *DB) RegenerateAPIToken(email string) (string, error) {
	newToken := generateToken()
	_, err := d.conn.Exec(`
		INSERT INTO users (email, api_token, updated_at) VALUES (?, ?, datetime('now'))
		ON CONFLICT(email) DO UPDATE SET api_token = excluded.api_token, updated_at = datetime('now')
	`, email, newToken)
	if err != nil {
		return "", err
	}
	return newToken, nil
}

// UserPreferences stores per-user preferences: default engine, export format, email notifications.
type UserPreferences struct {
	DefaultEngine       string `json:"default_engine"`
	DefaultExportFormat string `json:"default_export_format"`
	EmailNotifications  bool   `json:"email_notifications"`
}

// GetUserPreferences returns the user preferences, falling back to global defaults when unset.
func (d *DB) GetUserPreferences(email string) (*UserPreferences, error) {
	globalEngine, _, _ := d.GetEngineSettings()
	if globalEngine == "" {
		globalEngine = "gemini-3.8-flash"
	}
	prefs := &UserPreferences{
		DefaultEngine:       globalEngine,
		DefaultExportFormat: "html",
		EmailNotifications:  true,
	}

	var exportFmt sql.NullString
	var userEngine sql.NullString
	var emailNotif sql.NullInt64
	err := d.conn.QueryRow(`
		SELECT email_notifications, default_export_format, default_engine
		FROM users WHERE email = ?
	`, email).Scan(&emailNotif, &exportFmt, &userEngine)
	if err == sql.ErrNoRows {
		return prefs, nil
	}
	if err != nil {
		return nil, err
	}

	if emailNotif.Valid {
		prefs.EmailNotifications = (emailNotif.Int64 == 1)
	}
	if exportFmt.Valid && strings.TrimSpace(exportFmt.String) != "" {
		prefs.DefaultExportFormat = strings.TrimSpace(exportFmt.String)
	}
	if userEngine.Valid && strings.TrimSpace(userEngine.String) != "" {
		prefs.DefaultEngine = strings.TrimSpace(userEngine.String)
	}
	return prefs, nil
}

// UpdateUserPreferences updates the user preferences.
func (d *DB) UpdateUserPreferences(email string, defaultEngine, defaultExportFormat string, emailNotif bool) error {
	exportFmt := strings.ToLower(strings.TrimSpace(defaultExportFormat))
	if exportFmt == "" {
		exportFmt = "html"
	}
	engineVal := strings.TrimSpace(defaultEngine)
	notifVal := 0
	if emailNotif {
		notifVal = 1
	}

	_, err := d.conn.Exec(`
		INSERT INTO users (email, default_engine, default_export_format, email_notifications, updated_at)
		VALUES (?, ?, ?, ?, datetime('now'))
		ON CONFLICT(email) DO UPDATE SET
			default_engine = CASE WHEN excluded.default_engine != '' THEN excluded.default_engine ELSE users.default_engine END,
			default_export_format = excluded.default_export_format,
			email_notifications = excluded.email_notifications,
			updated_at = datetime('now')
	`, email, engineVal, exportFmt, notifVal)
	return err
}

