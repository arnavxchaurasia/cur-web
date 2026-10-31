// internal/db/report_shares.go
package db

import (
	"database/sql"
	"log"
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
		s.ExpiresAt = parseShareExpiry(s.Token, expiresAtStr.String)
	}
	return &s, nil
}

// parseShareExpiry turns a stored expiry into a time, failing CLOSED.
//
// A share link is served from the public, unauthenticated /api/reports/{token}
// route, and callers read a nil ExpiresAt as "never expires". So an expiry we
// cannot parse must not become nil — that would turn an expired link into a
// permanent one. We return a timestamp already in the past instead, which every
// caller reads as expired.
func parseShareExpiry(token, raw string) *time.Time {
	if t, err := parseSQLiteTime(raw); err == nil {
		return &t
	}
	log.Printf("[report_shares] token %q: unparseable expires_at %q; treating the share as expired", token, raw)
	past := time.Unix(0, 0).UTC()
	return &past
}

// ListReportShares returns all share rows, newest first.
func (d *DB) ListReportShares() ([]*ReportShare, error) {
	rows, err := d.conn.Query(
		`SELECT token, job_id, created_by, created_at, expires_at FROM report_shares ORDER BY created_at DESC`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var shares []*ReportShare
	for rows.Next() {
		var s ReportShare
		var createdBy sql.NullString
		var createdAtStr string
		var expiresAtStr sql.NullString
		if err := rows.Scan(&s.Token, &s.JobID, &createdBy, &createdAtStr, &expiresAtStr); err != nil {
			return nil, err
		}
		s.CreatedBy = createdBy.String
		if t, err := parseSQLiteTime(createdAtStr); err == nil {
			s.CreatedAt = t
		}
		if expiresAtStr.Valid && expiresAtStr.String != "" {
			s.ExpiresAt = parseShareExpiry(s.Token, expiresAtStr.String)
		}
		shares = append(shares, &s)
	}
	return shares, nil
}

// DeleteReportShare deletes the share row for token. Returns sql.ErrNoRows if
// no such token exists.
func (d *DB) DeleteReportShare(token string) error {
	res, err := d.conn.Exec(
		`DELETE FROM report_shares WHERE token = ?`, token,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}
