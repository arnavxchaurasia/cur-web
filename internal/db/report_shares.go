// internal/db/report_shares.go
package db

import (
	"database/sql"
	"time"
)

type ReportShare struct {
	Token     string     `json:"token"`
	JobID     string     `json:"job_id"`
	CreatedBy string     `json:"created_by"`
	CreatedAt time.Time  `json:"created_at"`
	ExpiresAt *time.Time `json:"expires_at,omitempty"`
}

func (d *DB) CreateReportShare(token, jobID, createdBy string, expiresAt time.Time) error {
	_, err := d.conn.Exec(
		`INSERT INTO report_shares (token, job_id, created_by, expires_at) VALUES (?, ?, ?, ?)`,
		token, jobID, createdBy, expiresAt.UTC().Format("2006-01-02 15:04:05"),
	)
	return err
}

// GetReportShare returns the share row for token, or (nil, nil) if it
// doesn't exist.
func (d *DB) GetReportShare(token string) (*ReportShare, error) {
	row := d.conn.QueryRow(
		`SELECT token, job_id, created_by, created_at, expires_at FROM report_shares WHERE token = ?`, token,
	)
	var s ReportShare
	var createdBy sql.NullString
	var createdAtStr string
	var expiresAtStr sql.NullString
	if err := row.Scan(&s.Token, &s.JobID, &createdBy, &createdAtStr, &expiresAtStr); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, err
	}
	s.CreatedBy = createdBy.String
	if t, err := parseSQLiteTime(createdAtStr); err == nil {
		s.CreatedAt = t
	}
	if expiresAtStr.Valid && expiresAtStr.String != "" {
		if t, err := parseSQLiteTime(expiresAtStr.String); err == nil {
			s.ExpiresAt = &t
		}
	}
	return &s, nil
}

func parseSQLiteTime(s string) (time.Time, error) {
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t, nil
	}
	return time.Parse("2006-01-02 15:04:05", s)
}
