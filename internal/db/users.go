// internal/db/users.go
package db

import "database/sql"

// UpsertUserSeen records that email successfully logged in, creating a
// `viewer`-role row on first sight and bumping updated_at on every
// subsequent login. Never touches an existing row's role — promotion/
// demotion only happens via SetUserRole.
func (d *DB) UpsertUserSeen(email string) error {
	_, err := d.conn.Exec(
		`INSERT INTO users (email, role) VALUES (?, 'viewer')
		 ON CONFLICT(email) DO UPDATE SET updated_at=datetime('now')`,
		email,
	)
	return err
}

// GetUserRole returns the role stored for email, or ("", nil) if no row
// exists yet (caller should fall back to the admin-email-list bootstrap).
func (d *DB) GetUserRole(email string) (string, error) {
	var role string
	err := d.conn.QueryRow(`SELECT role FROM users WHERE email = ?`, email).Scan(&role)
	if err == sql.ErrNoRows {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return role, nil
}

// SetUserRole creates or updates the role for email.
func (d *DB) SetUserRole(email, role string) error {
	_, err := d.conn.Exec(
		`INSERT INTO users (email, role) VALUES (?, ?)
		 ON CONFLICT(email) DO UPDATE SET role=excluded.role, updated_at=datetime('now')`,
		email, role,
	)
	return err
}

type User struct {
	Email     string `json:"email"`
	Role      string `json:"role"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

// ListUsers returns every known user, newest-seen first.
func (d *DB) ListUsers() ([]*User, error) {
	rows, err := d.conn.Query(`SELECT email, role, created_at, updated_at FROM users ORDER BY updated_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var users []*User
	for rows.Next() {
		var u User
		if err := rows.Scan(&u.Email, &u.Role, &u.CreatedAt, &u.UpdatedAt); err != nil {
			return nil, err
		}
		users = append(users, &u)
	}
	return users, nil
}
