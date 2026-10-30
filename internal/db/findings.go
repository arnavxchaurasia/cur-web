package db

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// ReviewFinding represents a structured advisory review finding.
type ReviewFinding struct {
	ID               string         `json:"id"`
	JobID            string         `json:"job_id"`
	AWSLIKey         string         `json:"aws_li_key"`
	CanonicalService string         `json:"canonical_service"`
	ChargeType       string         `json:"charge_type"`
	FindingType      string         `json:"finding_type"`
	Severity         string         `json:"severity"`
	AWSOD            float64        `json:"aws_od"`
	GCPOD            float64        `json:"gcp_od"`
	Ratio            float64        `json:"ratio"`
	EvidenceJSON     string         `json:"evidence_json"`
	Evidence         map[string]any `json:"evidence,omitempty"`
	SuggestedAction  string         `json:"suggested_action"`
	Status           string         `json:"status"` // pending | accepted | rejected | deferred
	ActionNote       *string        `json:"action_note,omitempty"`
	ResolvedBy       *string        `json:"resolved_by,omitempty"`
	ResolvedAt       *time.Time     `json:"resolved_at,omitempty"`
	CreatedAt        time.Time      `json:"created_at"`
}

// GenerateFindingID returns a deterministic ID for a job finding.
func GenerateFindingID(jobID, awsLIKey, findingType string) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("%s:%s:%s", jobID, awsLIKey, findingType)))
	return hex.EncodeToString(h[:16])
}

// SaveReviewFindings inserts or ignores structured review findings.
func (d *DB) SaveReviewFindings(findings []ReviewFinding) error {
	if len(findings) == 0 {
		return nil
	}
	tx, err := d.conn.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback()

	stmt, err := tx.Prepare(`INSERT OR IGNORE INTO review_findings (
		id, job_id, aws_li_key, canonical_service, charge_type, finding_type,
		severity, aws_od, gcp_od, ratio, evidence_json, suggested_action, status
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return fmt.Errorf("prepare insert review_findings: %w", err)
	}
	defer stmt.Close()

	for _, f := range findings {
		id := f.ID
		if id == "" {
			id = GenerateFindingID(f.JobID, f.AWSLIKey, f.FindingType)
		}
		status := f.Status
		if status == "" {
			status = "pending"
		}
		evJSON := f.EvidenceJSON
		if evJSON == "" && f.Evidence != nil {
			if b, err := json.Marshal(f.Evidence); err == nil {
				evJSON = string(b)
			}
		}
		if _, err := stmt.Exec(
			id, f.JobID, f.AWSLIKey, f.CanonicalService, f.ChargeType, f.FindingType,
			f.Severity, f.AWSOD, f.GCPOD, f.Ratio, evJSON, f.SuggestedAction, status,
		); err != nil {
			return fmt.Errorf("exec insert review_finding: %w", err)
		}
	}

	return tx.Commit()
}

// ListReviewFindings retrieves findings with optional jobID, service, severity, status filters.
func (d *DB) ListReviewFindings(jobID, service, severity, status string, limit, offset int) ([]ReviewFinding, int, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	if offset < 0 {
		offset = 0
	}

	where := "WHERE 1=1"
	var args []any

	if jobID != "" {
		where += " AND job_id = ?"
		args = append(args, jobID)
	}
	if service != "" {
		where += " AND canonical_service = ?"
		args = append(args, service)
	}
	if severity != "" {
		where += " AND severity = ?"
		args = append(args, severity)
	}
	if status != "" {
		where += " AND status = ?"
		args = append(args, status)
	}

	var count int
	countQuery := fmt.Sprintf("SELECT COUNT(*) FROM review_findings %s", where)
	if err := d.conn.QueryRow(countQuery, args...).Scan(&count); err != nil {
		return nil, 0, fmt.Errorf("count review_findings: %w", err)
	}

	query := fmt.Sprintf(`SELECT
		id, job_id, aws_li_key, canonical_service, charge_type, finding_type,
		severity, aws_od, gcp_od, ratio, evidence_json, suggested_action,
		status, action_note, resolved_by, resolved_at, created_at
		FROM review_findings %s ORDER BY aws_od DESC, created_at DESC LIMIT ? OFFSET ?`, where)

	queryArgs := append(args, limit, offset)
	rows, err := d.conn.Query(query, queryArgs...)
	if err != nil {
		return nil, 0, fmt.Errorf("query review_findings: %w", err)
	}
	defer rows.Close()

	var results []ReviewFinding
	for rows.Next() {
		var f ReviewFinding
		var resolvedAt sql.NullTime
		var actionNote, resolvedBy sql.NullString
		if err := rows.Scan(
			&f.ID, &f.JobID, &f.AWSLIKey, &f.CanonicalService, &f.ChargeType, &f.FindingType,
			&f.Severity, &f.AWSOD, &f.GCPOD, &f.Ratio, &f.EvidenceJSON, &f.SuggestedAction,
			&f.Status, &actionNote, &resolvedBy, &resolvedAt, &f.CreatedAt,
		); err != nil {
			return nil, 0, fmt.Errorf("scan review_finding: %w", err)
		}
		if actionNote.Valid {
			f.ActionNote = &actionNote.String
		}
		if resolvedBy.Valid {
			f.ResolvedBy = &resolvedBy.String
		}
		if resolvedAt.Valid {
			f.ResolvedAt = &resolvedAt.Time
		}
		if f.EvidenceJSON != "" {
			var ev map[string]any
			if err := json.Unmarshal([]byte(f.EvidenceJSON), &ev); err == nil {
				f.Evidence = ev
			}
		}
		results = append(results, f)
	}

	return results, count, nil
}

// GetReviewFindingByID retrieves a single finding by ID.
func (d *DB) GetReviewFindingByID(id string) (*ReviewFinding, error) {
	query := `SELECT
		id, job_id, aws_li_key, canonical_service, charge_type, finding_type,
		severity, aws_od, gcp_od, ratio, evidence_json, suggested_action,
		status, action_note, resolved_by, resolved_at, created_at
		FROM review_findings WHERE id = ?`

	var f ReviewFinding
	var resolvedAt sql.NullTime
	var actionNote, resolvedBy sql.NullString
	if err := d.conn.QueryRow(query, id).Scan(
		&f.ID, &f.JobID, &f.AWSLIKey, &f.CanonicalService, &f.ChargeType, &f.FindingType,
		&f.Severity, &f.AWSOD, &f.GCPOD, &f.Ratio, &f.EvidenceJSON, &f.SuggestedAction,
		&f.Status, &actionNote, &resolvedBy, &resolvedAt, &f.CreatedAt,
	); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("get review_finding: %w", err)
	}
	if actionNote.Valid {
		f.ActionNote = &actionNote.String
	}
	if resolvedBy.Valid {
		f.ResolvedBy = &resolvedBy.String
	}
	if resolvedAt.Valid {
		f.ResolvedAt = &resolvedAt.Time
	}
	if f.EvidenceJSON != "" {
		var ev map[string]any
		if err := json.Unmarshal([]byte(f.EvidenceJSON), &ev); err == nil {
			f.Evidence = ev
		}
	}
	return &f, nil
}

// UpdateReviewFindingAction updates finding status (accepted, rejected, deferred).
func (d *DB) UpdateReviewFindingAction(id, status, actionNote, resolvedBy string) error {
	now := time.Now().UTC()
	query := `UPDATE review_findings SET status = ?, action_note = ?, resolved_by = ?, resolved_at = ? WHERE id = ?`
	res, err := d.conn.Exec(query, status, actionNote, resolvedBy, now, id)
	if err != nil {
		return fmt.Errorf("update review_finding: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}
