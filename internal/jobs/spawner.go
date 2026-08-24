package jobs

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"github.com/facets/cur-web/internal/config"
	"github.com/facets/cur-web/internal/skill"
)

type SpawnerConfig struct {
	AGYPath  string
	AGYModel string // `agy --model` alias; defaults to "gemini-3.5-flash" if empty.
	// ANTIGRAVITY_API_KEY is intentionally not part of SpawnerConfig. The spawned
	// agy process inherits it from os.Environ(); set it on the host
	// (VM-wide on prod, shell on dev). GEMINI_API_KEY is ignored by agy —
	// use ANTIGRAVITY_API_KEY (same key value, different var name).
}

type SpawnResult struct {
	AWSSpend float64
	Err      error
}

type Spawner struct{ cfg SpawnerConfig }

func NewSpawner(cfg SpawnerConfig) *Spawner { return &Spawner{cfg: cfg} }

func (s *Spawner) Run(jobDir, inputExt string) SpawnResult {
	return s.RunWithSession(jobDir, inputExt, "")
}

// RunWithSession runs agy synchronously. sessionID is accepted for API
// compatibility but not forwarded to agy (--conversation is resume-only;
// fresh starts let agy assign its own ID). Test-only path.
func (s *Spawner) RunWithSession(jobDir, inputExt, _ string) SpawnResult {
	prompt := buildProjectionPrompt(inputExt)
	agyBin := s.agyBin()
	// --add-dir roots AGY's workspace at jobDir (see startDetached for why).
	args := append(s.baseFlags(), "--add-dir", jobDir, "-p", prompt)
	cmd := exec.Command(agyBin, args...)
	cmd.Dir = jobDir
	cmd.Env = s.skillEnv()

	logPath := filepath.Join(jobDir, "agy.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		return SpawnResult{Err: fmt.Errorf("create log: %w", err)}
	}
	defer logFile.Close()
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	if err := cmd.Run(); err != nil {
		tail := tailLog(logPath, 500)
		return SpawnResult{Err: fmt.Errorf("agy failed: %s", tail)}
	}

	reportPath := filepath.Join(jobDir, "report.html")
	if _, err := os.Stat(reportPath); err != nil {
		tail := tailLog(logPath, 500)
		return SpawnResult{Err: fmt.Errorf("report.html not found: %s", tail)}
	}

	content, _ := os.ReadFile(reportPath)
	// Synchronous Run path is only used by tests; the production async path
	// (Start + Watcher.finalize) reads aws_spend from projection.duckdb.
	spend := extractAWSSpendFromHTML(string(content))
	return SpawnResult{AWSSpend: spend}
}

// Start spawns a fresh projection run. Instead of one agy call that
// self-orchestrates all six phases (unreliable on Gemini), it launches the
// run_all.py orchestrator, which drives the phases one at a time with tight
// per-phase prompts and a deterministic DuckDB gate between each. See
// orchestrate.go. The watcher polls the returned PID (the orchestrator) and
// finalizes when the report appears — same contract as before.
func (s *Spawner) Start(jobDir, inputExt, _ string) (int, error) {
	return s.StartOrchestrated(jobDir, inputExt)
}

// StartRefine resumes the job's most recent agy session via --continue so the
// agent keeps the full conversational context of the original run, then feeds
// it the user's refinement instruction. --continue resumes the most recent
// conversation in the CWD (each job dir is isolated, so this is unambiguous).
// The DuckDB state also persists on disk as a fallback pointer.
func (s *Spawner) StartRefine(jobDir, _, instruction string) (int, error) {
	prompt := "Refine an existing AWS-to-GCP cost projection. " +
		"The projection state is in projection-audit/projection.duckdb in this directory. " +
		"Refinement instruction from the user: " + instruction + ". " +
		"Using the aws-gcp-cost-projection-gemini skill: update aws_li_to_gcp_li for affected " +
		"rows, recompute gcp_projection, then re-run Phase 6 to produce a new versioned " +
		"report. Keep all unrelated mappings unchanged. " +
		"IMPORTANT: run_results column meaning is fixed — gcp_od is always GCP On-Demand, " +
		"gcp_1yr_cud is always 1-year CUD, gcp_3yr_cud is always 3-year CUD. Display " +
		"instructions (e.g. 'use 3yr CUD as primary') change report layout only; they " +
		"NEVER permute run_results schema columns. Always use the named-column " +
		"INSERT (col_list) SELECT ... AS alias form per Phase 6. Report when done."
	args := append(s.baseFlags(), "--continue", "-p", prompt)
	return s.startDetached(jobDir, args)
}

// baseFlags are the agy CLI flags common to every spawn:
//
//	--dangerously-skip-permissions : auto-approve all tool calls (no interactive prompts)
//	--model <alias>                : from SpawnerConfig.AGYModel (default "gemini-3.5-flash").
//	--print-timeout 45m            : override agy's default 5-minute print-mode timeout;
//	                                 large bills take 20-40 min across 6 phases.
func (s *Spawner) baseFlags() []string {
	model := s.cfg.AGYModel
	if model == "" {
		model = config.DefaultAGYModel
	}
	return []string{"--dangerously-skip-permissions", "--model", model, "--print-timeout", config.AGYPrintTimeout()}
}

// startDetached is the shared spawn machinery: Setsid for true detachment,
// stdout/stderr → agy.log (append, not truncate, so refinement output
// follows the original run's log), Process.Release so we never block on Wait.
func (s *Spawner) startDetached(jobDir string, args []string) (int, error) {
	// --log-file captures AGY's internal errors (quota, auth) that never reach
	// stdout/stderr — without it, quota exhaustion looks like a silent crash.
	args = append(args, "--log-file", filepath.Join(jobDir, "agy-internal.log"))
	// --add-dir makes jobDir AGY's workspace root. WITHOUT it, AGY runs every
	// tool command in its shared scratch sandbox (~/.gemini/antigravity-cli/scratch),
	// ignoring cmd.Dir — so progress.json, projection.duckdb, and report.html all
	// land in scratch and the watcher (which waits on jobDir/report.html) never
	// sees them. The shared scratch also leaks artifacts between the two AGY repos.
	args = append(args, "--add-dir", jobDir)

	if err := exec.Command("git", "init", jobDir).Run(); err != nil {
		return 0, fmt.Errorf("git init failed: %w", err)
	}
	skillDir := s.resolveSkillDir()
	if err := exec.Command("cp", "-r", filepath.Join(skillDir, "phases"), jobDir).Run(); err != nil {
		return 0, fmt.Errorf("copy phases failed: %w", err)
	}
	if err := exec.Command("cp", "-r", filepath.Join(skillDir, "scripts"), jobDir).Run(); err != nil {
		return 0, fmt.Errorf("copy scripts failed: %w", err)
	}

	cmd := exec.Command(s.agyBin(), args...)
	cmd.Dir = jobDir
	cmd.Env = s.skillEnv()
	cmd.SysProcAttr = sysProcAttr

	logFile, err := os.OpenFile(
		filepath.Join(jobDir, "agy.log"),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644,
	)
	if err != nil {
		return 0, fmt.Errorf("open log: %w", err)
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	if err := cmd.Start(); err != nil {
		logFile.Close()
		return 0, fmt.Errorf("start agy: %w", err)
	}
	pid := cmd.Process.Pid
	cmd.Process.Release()
	logFile.Close() // child inherited the fd; close parent's copy to avoid leak
	return pid, nil
}

func (s *Spawner) agyBin() string {
	if s.cfg.AGYPath != "" {
		return s.cfg.AGYPath
	}
	return "agy"
}

func (s *Spawner) resolveSkillDir() string {
	return skill.ResolveDir()
}

// skillEnv returns os.Environ() plus SKILL_DIR pointing to the installed skill.
// The skill's bash scripts (preflight.sh, scripts/find-sku.sh) use $SKILL_DIR
// to resolve their own paths, since the agent's CWD is jobDir, not the skill dir.
func (s *Spawner) skillEnv() []string {
	return append(os.Environ(), "SKILL_DIR="+s.resolveSkillDir())
}

func buildProjectionPrompt(inputExt string) string {
	return fmt.Sprintf(
		"Use the aws-gcp-cost-projection-gemini skill to project costs from ./input%s. "+
			"Do not invoke any other skills — only aws-gcp-cost-projection-gemini. "+
			"If a skill other than aws-gcp-cost-projection-gemini seems relevant, ignore it. "+
			"If the input cannot be processed for a structural reason (wrong cloud — "+
			"e.g. Azure or GCP bill rather than AWS — corrupted file, unknown shape "+
			"that even the skill's PDF/CUR/CSV branches can't make sense of), write "+
			"a single short paragraph explaining why to ./failure.txt and exit. "+
			"Do not retry on a structural failure.",
		inputExt,
	)
}

func tailLog(path string, n int) string {
	b, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		return ""
	}
	if len(b) > n {
		b = b[len(b)-n:]
	}
	return string(b)
}
