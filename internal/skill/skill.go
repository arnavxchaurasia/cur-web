// Package skill provides the canonical skill-directory resolution used by the
// spawner, watcher, and preflight checks. All three must use the same logic so
// $SKILL_DIR overrides apply everywhere and the fallback path never drifts.
package skill

import (
	"os"
	"path/filepath"
)

// RepoDirName is the skill folder name as it lives in this repo (skill/<RepoDirName>)
// and inside the deploy tarball. server-deploy.sh installs it as InstalledName.
const RepoDirName = "aws-gcp-cost-projection"

// Name (a.k.a. InstalledName) is the skill folder name under
// ~/.gemini/antigravity-cli/skills/ once deployed. The deploy adds a "-gemini"
// suffix to distinguish it from the upstream Claude skill on shared machines.
const Name = RepoDirName + "-gemini"

// ResolveDir returns the skill directory, checking in priority order:
//  1. $SKILL_DIR env var  — explicit override for CI / alternate installs
//  2. ./skill/aws-gcp-cost-projection relative to cwd — dev / monorepo layout
//  3. ~/.gemini/antigravity-cli/skills/<Name> — standard install path
func ResolveDir() string {
	if envDir := os.Getenv("SKILL_DIR"); envDir != "" {
		return envDir
	}
	if cwd, err := os.Getwd(); err == nil {
		local := filepath.Join(cwd, "skill", RepoDirName)
		if stat, err := os.Stat(local); err == nil && stat.IsDir() {
			return local
		}
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".gemini", "antigravity-cli", "skills", Name)
}
