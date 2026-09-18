package jobs

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/facets/cur-web/internal/skill"
)

const catalogStaleDays = 7

// StartCatalogRefresher launches a background goroutine that keeps the GCP
// SKU catalog fresh. It checks catalog age on startup and then every week;
// if the catalog is older than catalogStaleDays it runs
// scripts/catalog_health_check.py --refresh inside the skill directory.
//
// Requires Python 3 on PATH and GCP credentials (GOOGLE_CLOUD_API_KEY env var
// or `gcloud auth login`). If credentials are absent the health-check script
// runs in audit-only mode and logs the gap without failing the server.
func StartCatalogRefresher(ctx context.Context) {
	go func() {
		// Run once immediately at startup, then on a weekly ticker.
		runCatalogRefreshIfStale()

		ticker := time.NewTicker(24 * time.Hour)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				runCatalogRefreshIfStale()
			}
		}
	}()
}

// catalogAge returns how many days ago the catalog was last fetched.
// Returns a large number if CATALOG_META.json is missing or unreadable.
func catalogAge() int {
	skillDir := skill.ResolveDir()
	metaPath := filepath.Join(skillDir, "data", "CATALOG_META.json")

	data, err := os.ReadFile(metaPath)
	if err != nil {
		return 9999
	}
	var meta struct {
		FetchedAt string `json:"fetched_at"`
	}
	if err := json.Unmarshal(data, &meta); err != nil || meta.FetchedAt == "" {
		return 9999
	}
	ts, err := time.Parse(time.RFC3339, meta.FetchedAt)
	if err != nil {
		return 9999
	}
	return int(time.Since(ts).Hours() / 24)
}

func runCatalogRefreshIfStale() {
	age := catalogAge()
	if age < catalogStaleDays {
		slog.Debug("catalog is fresh, skipping refresh", "age_days", age)
		return
	}
	runCatalogRefreshNow()
}

// runCatalogRefreshNow runs the health-check/refresh script unconditionally,
// ignoring catalogStaleDays. Used by the forced "refresh now" admin action.
func runCatalogRefreshNow() {
	skillDir := skill.ResolveDir()
	script := filepath.Join(skillDir, "scripts", "catalog_health_check.py")
	if _, err := os.Stat(script); err != nil {
		slog.Warn("catalog_health_check.py not found, skipping refresh", "path", script)
		return
	}

	slog.Info("refreshing GCP catalog", "age_days", catalogAge())

	python := "python3"
	if _, err := exec.LookPath(python); err != nil {
		python = "python"
	}

	cmd := exec.Command(python, script, "--refresh", "--skill-dir", skillDir)
	cmd.Dir = skillDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		slog.Warn("catalog refresh finished with error", "err", err)
	} else {
		slog.Info("GCP catalog refresh complete")
	}
}

// CatalogStatus is the admin-facing view of GCP catalog freshness.
type CatalogStatus struct {
	AgeDays int  `json:"age_days"`
	Stale   bool `json:"stale"`
}

// GetCatalogStatus reports the current catalog age and staleness. Exported
// so the admin catalog-status handler can surface it without duplicating
// the CATALOG_META.json read.
func GetCatalogStatus() CatalogStatus {
	age := catalogAge()
	return CatalogStatus{AgeDays: age, Stale: age >= catalogStaleDays}
}

// TriggerCatalogRefresh runs a forced catalog refresh in the background
// (ignoring the staleness threshold) and returns immediately. Used by the
// admin "refresh now" action.
func TriggerCatalogRefresh() {
	go runCatalogRefreshNow()
}
