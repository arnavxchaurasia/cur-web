package db

import (
	"database/sql"
	"time"
)

type Job struct {
	ID          string     `json:"id"`
	Owner       string     `json:"owner"`
	Prospect    string     `json:"prospect"`
	Status      string     `json:"status"`
	InputExt    string     `json:"input_ext"`
	AWSSpend    *float64   `json:"aws_spend"`
	Error       string     `json:"error"`
	SessionID   string     `json:"session_id"`
	AgentPID    int        `json:"agent_pid"`
	Attempts    int        `json:"attempts"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
	CancelledAt *time.Time `json:"cancelled_at,omitempty"`
}

func (d *DB) CreateJob(id, owner, prospect, inputExt, sessionID string) error {
	_, err := d.conn.Exec(
		`INSERT INTO jobs (id, owner, prospect, input_ext, session_id) VALUES (?, ?, ?, ?, ?)`,
		id, owner, prospect, inputExt, sessionID,
	)
	return err
}

func (d *DB) UpdateJobSessionID(id, sessionID string) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET session_id=?, updated_at=datetime('now') WHERE id=?`,
		sessionID, id,
	)
	return err
}

func (d *DB) UpdateJobPID(id string, pid int) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET agent_pid=?, updated_at=datetime('now') WHERE id=?`,
		pid, id,
	)
	return err
}

func (d *DB) IncrementJobAttempts(id string) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET attempts = attempts + 1, updated_at=datetime('now') WHERE id=?`,
		id,
	)
	return err
}

// ResetJobForRetry clears terminal state (status, error, aws_spend, attempts)
// and assigns a fresh session_id. Used when a user manually retries a failed job.
//
// attempts is set to 1 (not 0) because this call itself is the first spawn —
// Watch() will increment on each subsequent failure, so the job gets
// (maxAttempts - 1) additional automatic retries. Fresh jobs start at 0, so
// they get one more total attempt than manual retries; this asymmetry is
// intentional (manual retry already had one run, fresh jobs have not).
func (d *DB) ResetJobForRetry(id, sessionID string) error {
	_, err := d.conn.Exec(
		`UPDATE jobs
		 SET status='running', error='', aws_spend=NULL,
		     attempts=1, agent_pid=0, session_id=?,
		     updated_at=datetime('now')
		 WHERE id=?`,
		sessionID, id,
	)
	return err
}

func (d *DB) ListNonTerminalJobs() ([]*Job, error) {
	rows, err := d.conn.Query(
		`SELECT id, owner, prospect, status, input_ext, aws_spend, error, session_id, agent_pid, attempts, created_at, updated_at
		 FROM jobs WHERE status IN ('pending', 'running') ORDER BY created_at`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var jobs []*Job
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return nil, err
		}
		jobs = append(jobs, j)
	}
	return jobs, nil
}

func (d *DB) GetJob(id string) (*Job, error) {
	row := d.conn.QueryRow(
		`SELECT id, owner, prospect, status, input_ext, aws_spend, error, session_id, agent_pid, attempts, created_at, updated_at
		 FROM jobs WHERE id = ?`, id,
	)
	return scanJob(row)
}

// ListAllJobs returns every job in the table, newest first. Admin-only —
// the handler wrapping this is responsible for the access check.
func (d *DB) ListAllJobs() ([]*Job, error) {
	rows, err := d.conn.Query(
		`SELECT id, owner, prospect, status, input_ext, aws_spend, error, session_id, agent_pid, attempts, created_at, updated_at
		 FROM jobs ORDER BY created_at DESC`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var jobs []*Job
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return nil, err
		}
		jobs = append(jobs, j)
	}
	return jobs, nil
}

func (d *DB) ListJobsByOwner(owner string) ([]*Job, error) {
	rows, err := d.conn.Query(
		`SELECT id, owner, prospect, status, input_ext, aws_spend, error, session_id, agent_pid, attempts, created_at, updated_at
		 FROM jobs WHERE owner = ? ORDER BY created_at DESC`, owner,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var jobs []*Job
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return nil, err
		}
		jobs = append(jobs, j)
	}
	return jobs, nil
}

func (d *DB) UpdateJobRunning(id string) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET status='running', updated_at=datetime('now') WHERE id=?`, id,
	)
	return err
}

func (d *DB) UpdateJobDone(id string, spend float64) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET status='done', aws_spend=?, updated_at=datetime('now') WHERE id=?`,
		spend, id,
	)
	return err
}

func (d *DB) UpdateJobFailed(id, errMsg string) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET status='failed', error=?, updated_at=datetime('now') WHERE id=?`,
		errMsg, id,
	)
	return err
}

func (d *DB) UpdateJobCancelled(id string) error {
	_, err := d.conn.Exec(
		`UPDATE jobs SET status='cancelled', cancelled_at=datetime('now'), updated_at=datetime('now') WHERE id=?`,
		id,
	)
	return err
}

// DeleteJob removes a job row. Callers must ensure the job is in a terminal
// state (done/failed/cancelled) before calling this.
func (d *DB) DeleteJob(id string) error {
	_, err := d.conn.Exec(`DELETE FROM jobs WHERE id=?`, id)
	return err
}

// ListAllJobsFiltered supports the admin jobs page: pagination plus optional
// status/date-range filtering. Any of status/from/to may be empty/nil to skip
// that filter.
func (d *DB) ListAllJobsFiltered(limit, offset int, status string, from, to *time.Time) ([]*Job, error) {
	q := `SELECT id, owner, prospect, status, input_ext, aws_spend, error, session_id, agent_pid, attempts, created_at, updated_at
	      FROM jobs WHERE 1=1`
	var args []any
	if status != "" {
		q += ` AND status = ?`
		args = append(args, status)
	}
	if from != nil {
		q += ` AND created_at >= ?`
		args = append(args, from.UTC().Format("2006-01-02 15:04:05"))
	}
	if to != nil {
		q += ` AND created_at <= ?`
		args = append(args, to.UTC().Format("2006-01-02 15:04:05"))
	}
	q += ` ORDER BY created_at DESC LIMIT ? OFFSET ?`
	args = append(args, limit, offset)

	rows, err := d.conn.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var jobs []*Job
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return nil, err
		}
		jobs = append(jobs, j)
	}
	return jobs, nil
}

func (d *DB) CountAllJobs(status string, from, to *time.Time) (int, error) {
	q := `SELECT COUNT(*) FROM jobs WHERE 1=1`
	var args []any
	if status != "" {
		q += ` AND status = ?`
		args = append(args, status)
	}
	if from != nil {
		q += ` AND created_at >= ?`
		args = append(args, from.UTC().Format("2006-01-02 15:04:05"))
	}
	if to != nil {
		q += ` AND created_at <= ?`
		args = append(args, to.UTC().Format("2006-01-02 15:04:05"))
	}
	var count int
	err := d.conn.QueryRow(q, args...).Scan(&count)
	return count, err
}

func (d *DB) LogAdminAction(actorEmail, action, targetID, details string) error {
	_, err := d.conn.Exec(
		`INSERT INTO admin_audit (actor_email, action, target_id, details) VALUES (?, ?, ?, ?)`,
		actorEmail, action, targetID, details,
	)
	return err
}

type AuditEntry struct {
	ID         int64     `json:"id"`
	ActorEmail string    `json:"actor_email"`
	Action     string    `json:"action"`
	TargetID   string    `json:"target_id"`
	Details    string    `json:"details"`
	CreatedAt  time.Time `json:"created_at"`
}

func (d *DB) ListAuditLog(limit, offset int) ([]*AuditEntry, error) {
	rows, err := d.conn.Query(
		`SELECT id, actor_email, action, target_id, details, created_at
		 FROM admin_audit ORDER BY created_at DESC LIMIT ? OFFSET ?`, limit, offset,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []*AuditEntry
	for rows.Next() {
		var e AuditEntry
		var targetID, details sql.NullString
		var createdAtStr string
		if err := rows.Scan(&e.ID, &e.ActorEmail, &e.Action, &targetID, &details, &createdAtStr); err != nil {
			return nil, err
		}
		e.TargetID = targetID.String
		e.Details = details.String
		createdAt, err := time.Parse(time.RFC3339, createdAtStr)
		if err != nil {
			createdAt, err = time.Parse("2006-01-02 15:04:05", createdAtStr)
			if err != nil {
				return nil, err
			}
		}
		e.CreatedAt = createdAt
		entries = append(entries, &e)
	}
	return entries, nil
}

type UsageStats struct {
	TotalJobs        int                `json:"total_jobs"`
	DoneJobs         int                `json:"done_jobs"`
	FailedJobs       int                `json:"failed_jobs"`
	FailureRate      float64            `json:"failure_rate"`
	AvgProcessingSec float64            `json:"avg_processing_seconds"`
	JobsByDay        []DayCount         `json:"jobs_by_day"`
}

type DayCount struct {
	Day   string `json:"day"`
	Count int    `json:"count"`
}

// GetUsageStats reports jobs/day for the last 30 days, overall failure rate,
// and average processing time for completed jobs.
func (d *DB) GetUsageStats() (*UsageStats, error) {
	stats := &UsageStats{}
	if err := d.conn.QueryRow(`SELECT COUNT(*) FROM jobs`).Scan(&stats.TotalJobs); err != nil {
		return nil, err
	}
	if err := d.conn.QueryRow(`SELECT COUNT(*) FROM jobs WHERE status='done'`).Scan(&stats.DoneJobs); err != nil {
		return nil, err
	}
	if err := d.conn.QueryRow(`SELECT COUNT(*) FROM jobs WHERE status='failed'`).Scan(&stats.FailedJobs); err != nil {
		return nil, err
	}
	if stats.TotalJobs > 0 {
		stats.FailureRate = float64(stats.FailedJobs) / float64(stats.TotalJobs)
	}

	var avgSec sql.NullFloat64
	err := d.conn.QueryRow(
		`SELECT AVG((julianday(updated_at) - julianday(created_at)) * 86400.0)
		 FROM jobs WHERE status='done'`,
	).Scan(&avgSec)
	if err != nil {
		return nil, err
	}
	if avgSec.Valid {
		stats.AvgProcessingSec = avgSec.Float64
	}

	rows, err := d.conn.Query(
		`SELECT date(created_at) as day, COUNT(*) FROM jobs
		 WHERE created_at >= datetime('now', '-30 days')
		 GROUP BY day ORDER BY day`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var dc DayCount
		if err := rows.Scan(&dc.Day, &dc.Count); err != nil {
			return nil, err
		}
		stats.JobsByDay = append(stats.JobsByDay, dc)
	}
	return stats, nil
}

type scanner interface {
	Scan(dest ...any) error
}

func scanJob(s scanner) (*Job, error) {
	var j Job
	var spend sql.NullFloat64
	var errStr sql.NullString
	var sessionID sql.NullString
	var agentPID sql.NullInt64
	var attempts sql.NullInt64
	var createdAtStr string
	var updatedAtStr string

	err := s.Scan(&j.ID, &j.Owner, &j.Prospect, &j.Status, &j.InputExt,
		&spend, &errStr, &sessionID, &agentPID, &attempts, &createdAtStr, &updatedAtStr)
	if err != nil {
		return nil, err
	}

	// Parse SQLite datetime strings (modernc.org/sqlite returns ISO8601)
	createdAt, err := time.Parse(time.RFC3339, createdAtStr)
	if err != nil {
		// Try alternate format
		createdAt, err = time.Parse("2006-01-02 15:04:05", createdAtStr)
		if err != nil {
			return nil, err
		}
	}

	updatedAt, err := time.Parse(time.RFC3339, updatedAtStr)
	if err != nil {
		// Try alternate format
		updatedAt, err = time.Parse("2006-01-02 15:04:05", updatedAtStr)
		if err != nil {
			return nil, err
		}
	}

	if spend.Valid {
		j.AWSSpend = &spend.Float64
	}
	if errStr.Valid {
		j.Error = errStr.String
	}
	if sessionID.Valid {
		j.SessionID = sessionID.String
	}
	if agentPID.Valid {
		j.AgentPID = int(agentPID.Int64)
	}
	if attempts.Valid {
		j.Attempts = int(attempts.Int64)
	}
	j.CreatedAt = createdAt
	j.UpdatedAt = updatedAt
	return &j, nil
}
