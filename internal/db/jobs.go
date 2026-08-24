package db

import (
	"database/sql"
	"time"
)

type Job struct {
	ID        string    `json:"id"`
	Owner     string    `json:"owner"`
	Prospect  string    `json:"prospect"`
	Status    string    `json:"status"`
	InputExt  string    `json:"input_ext"`
	AWSSpend  *float64  `json:"aws_spend"`
	Error     string    `json:"error"`
	SessionID string    `json:"session_id"`
	AgentPID  int       `json:"agent_pid"`
	Attempts  int       `json:"attempts"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
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
