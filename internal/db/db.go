package db

import (
	"database/sql"
	"strings"

	_ "modernc.org/sqlite" // registers the sqlite3 driver for database/sql
)

type DB struct {
	conn *sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    prospect   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    input_ext  TEXT,
    aws_spend  REAL,
    error      TEXT,
    session_id TEXT,
    agent_pid INTEGER DEFAULT 0,
    attempts   INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT (datetime('now')),
    updated_at DATETIME DEFAULT (datetime('now'))
);`

func Open(path string) (*DB, error) {
	// WAL + a 5s busy_timeout so concurrent writers (the per-job watcher
	// goroutines and the HTTP handlers) don't hit an instant SQLITE_BUSY. Without
	// this a lost UpdateJobDone leaves a finished job stuck showing "running".
	// Pin to a single connection so writes serialize cleanly through WAL.
	conn, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)")
	if err != nil {
		return nil, err
	}
	conn.SetMaxOpenConns(1)
	if _, err := conn.Exec(schema); err != nil {
		return nil, err
	}
	// Idempotent migrations for pre-existing dbs.
	// Ignored errors: "duplicate column" = already applied.
	for _, ddl := range []string{
		`ALTER TABLE jobs ADD COLUMN session_id TEXT`,
		`ALTER TABLE jobs ADD COLUMN agent_pid INTEGER DEFAULT 0`,
		`ALTER TABLE jobs ADD COLUMN attempts INTEGER DEFAULT 1`,
	} {
		if _, err := conn.Exec(ddl); err != nil {
			msg := err.Error()
			if !strings.Contains(msg, "duplicate column") && !strings.Contains(msg, "already exists") {
				return nil, err
			}
		}
	}
	return &DB{conn: conn}, nil
}

func (d *DB) Close() error {
	return d.conn.Close()
}
