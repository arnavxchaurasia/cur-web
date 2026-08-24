package preflight

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/facets/cur-web/internal/skill"
)

// Check verifies that the host has everything the server needs before it
// starts accepting jobs.
func Check() error {
	// 1. agy on PATH
	if _, err := exec.LookPath("agy"); err != nil {
		return fmt.Errorf("agy not found on PATH: %w", err)
	}
	// 2. skill installed — uses the same resolution logic as the spawner
	skillDir := skill.ResolveDir()
	skillMD := filepath.Join(skillDir, "SKILL.md")
	if _, err := os.Stat(skillMD); err != nil {
		return fmt.Errorf(
			"skill not found at %s — run: rsync -a <path-to-skill-dir>/ %s/: %w",
			skillMD, skillDir, err,
		)
	}
	// 3. required tools for the skill pipeline
	for _, tool := range []string{"python3", "duckdb", "jq", "gzip"} {
		if _, err := exec.LookPath(tool); err != nil {
			return fmt.Errorf("required tool %q not found on PATH: %w", tool, err)
		}
	}
	// 4. key scripts are readable (catches missing rsync or permission issues early)
	for _, rel := range []string{"scripts/ingest.py", "scripts/validate_fix.py"} {
		p := filepath.Join(skillDir, rel)
		if _, err := os.Stat(p); err != nil {
			return fmt.Errorf("required skill script not found: %s: %w", p, err)
		}
	}
	return nil
}
