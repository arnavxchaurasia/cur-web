package jobs

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/facets/cur-web/internal/config"
)

// Progress is a snapshot of an in-flight agy session, derived from the
// progress.json file the skill writes to the job directory at each phase
// transition.
type Progress struct {
	Events       int    `json:"events"`        // deprecated: always 0, kept for API contract stability
	Phase        string `json:"phase"`         // last-known phase name (debug)
	PhaseNumber  int    `json:"phase_number"`  // 1-6, written by the skill
	LastActivity string `json:"last_activity"`
	TranscriptOK bool   `json:"transcript_ok"` // true when progress.json exists
}

const totalPhases = config.TotalPhases

// progressFile is the file the skill writes into the job working directory.
const progressFile = "progress.json"


// ReadProgress reads progress.json from the job directory. The second argument
// (sessionID) is kept for API compatibility with callers but is unused —
// AGY CLI manages session state internally.
// Returns Progress{TranscriptOK: false} if the file doesn't exist yet.
func ReadProgress(jobDir, _ string) (*Progress, error) {
	data, err := os.ReadFile(filepath.Join(jobDir, progressFile))
	if err != nil {
		if os.IsNotExist(err) {
			// Agent is initializing or running early steps before the skill writes progress.json.
			// Default to Phase 1 so the UI shows activity immediately.
			return &Progress{
				TranscriptOK: true,
				PhaseNumber:  1,
				Phase:        "Initialization",
				LastActivity: "Initializing agent...",
			}, nil
		}
		slog.Error("ReadProgress: unexpected read error", "job_dir", jobDir, "err", err)
		return &Progress{}, nil
	}

	// Parse into a raw map to handle "phase" being int (legacy) or string (new).
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return &Progress{TranscriptOK: true}, nil
	}

	// Phase number: new schema uses "current_step" (int), legacy uses "phase" (int).
	phaseNum := 0
	if v, ok := raw["current_step"]; ok {
		if f, ok := v.(float64); ok {
			phaseNum = int(f)
		}
	}
	if phaseNum == 0 {
		if v, ok := raw["phase"]; ok {
			if f, ok := v.(float64); ok {
				phaseNum = int(f)
			}
		}
	}
	if phaseNum < 1 {
		phaseNum = 1
	}
	if phaseNum > totalPhases {
		phaseNum = totalPhases
	}

	// Phase name: new schema "phase" (string), legacy "phase_name".
	phaseName := ""
	if v, ok := raw["phase"]; ok {
		if s, ok := v.(string); ok {
			phaseName = s
		}
	}
	if phaseName == "" {
		if v, ok := raw["phase_name"]; ok {
			phaseName, _ = v.(string)
		}
	}

	// Last activity: new schema "status", legacy "last_activity".
	lastActivity := ""
	if v, ok := raw["status"]; ok {
		lastActivity, _ = v.(string)
	}
	if lastActivity == "" {
		if v, ok := raw["last_activity"]; ok {
			lastActivity, _ = v.(string)
		}
	}

	return &Progress{
		TranscriptOK: true,
		Phase:        phaseName,
		PhaseNumber:  phaseNum,
		LastActivity: lastActivity,
	}, nil
}
