package db

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/facets/cur-web/internal/skill"
)

// RatioGroup is a pair of assumptions whose quotient must be a machine shape
// GCP actually sells (e.g. 1 vCPU : 8 GiB). Validating each value on its own is
// not enough: 0.1 vCPU and 64 GiB are individually plausible and together a
// 1:640 machine that does not exist.
type RatioGroup struct {
	VCPUKey string `json:"vcpu_key"`
	RAMKey  string `json:"ram_key"`
	Label   string `json:"label"`
}

// RatioRegistry is the shared source of truth for allowed vCPU:RAM ratios. It
// is generated from scripts/candidate_pool.py by scripts/gen_ratio_registry.py
// so that adding a GCP machine family widens the allowed set without a code
// change on either side.
type RatioRegistry struct {
	AllowedRAMPerVCPU []float64    `json:"allowed_ram_per_vcpu"`
	RatioGroups       []RatioGroup `json:"ratio_groups"`
}

var (
	ratioRegistryOnce sync.Once
	ratioRegistry     *RatioRegistry
	ratioRegistryErr  error
)

// ratioGroupKeys lists the assumption keys that participate in some ratio
// group. It exists only so that a missing registry can refuse writes to those
// keys while leaving every unrelated assumption editable — the allowed RATIOS
// are never hardcoded here, only the structural fact that these keys pair up.
var ratioGroupKeys = map[string]bool{
	"aurora_acu_vcpu":    true,
	"aurora_acu_ram_gib": true,
}

// ratioRegistryPaths lists, in order, where the registry may live: next to the
// resolved skill directory in a deployed install, or repo-relative when running
// from a checkout or a package test.
func ratioRegistryPaths() []string {
	rel := filepath.Join("skill", "aws-gcp-cost-projection", "data", "ratio_registry.json")
	return []string{
		filepath.Join(skill.ResolveDir(), "data", "ratio_registry.json"),
		rel,
		filepath.Join("..", "..", rel),
	}
}

// LoadRatioRegistry reads data/ratio_registry.json once per process.
func LoadRatioRegistry() (*RatioRegistry, error) {
	ratioRegistryOnce.Do(func() {
		var raw []byte
		var tried []string
		for _, path := range ratioRegistryPaths() {
			b, err := os.ReadFile(path)
			if err == nil {
				raw = b
				break
			}
			tried = append(tried, path)
		}
		if raw == nil {
			ratioRegistryErr = fmt.Errorf("ratio registry not found (looked in: %s)", strings.Join(tried, ", "))
			return
		}
		var reg RatioRegistry
		if err := json.Unmarshal(raw, &reg); err != nil {
			ratioRegistryErr = fmt.Errorf("ratio registry malformed: %w", err)
			return
		}
		if len(reg.AllowedRAMPerVCPU) == 0 {
			ratioRegistryErr = fmt.Errorf("ratio registry lists no allowed ratios")
			return
		}
		ratioRegistry = &reg
	})
	return ratioRegistry, ratioRegistryErr
}

// FormatAllowed renders the allowed ratios for an error message: "1:4, 1:8, 1:16".
func (r *RatioRegistry) FormatAllowed() string {
	out := ""
	for i, v := range r.AllowedRAMPerVCPU {
		if i > 0 {
			out += ", "
		}
		out += fmt.Sprintf("1:%g", v)
	}
	return out
}

// IsAllowedRatio reports whether ratio matches one of the registry's shapes.
func (r *RatioRegistry) IsAllowedRatio(ratio float64) bool {
	for _, allowed := range r.AllowedRAMPerVCPU {
		if math.Abs(ratio-allowed) < 1e-3 {
			return true
		}
	}
	return false
}

// validateRatioGroups rejects an assumption write whose resulting vCPU:RAM
// ratio is not a shape GCP sells. key/value is the value being written; the
// partner value is read from the database.
//
// It fails closed: if the registry cannot be loaded, the write is refused
// rather than allowed through unvalidated.
func (d *DB) validateRatioGroups(key string, value float64) error {
	reg, err := LoadRatioRegistry()
	if err != nil {
		// Fail closed for the keys that need validating; leave every other
		// assumption editable rather than taking the admin screen down.
		if ratioGroupKeys[key] {
			return fmt.Errorf("cannot validate ratio for %s: %w", key, err)
		}
		return nil
	}
	for _, g := range reg.RatioGroups {
		var partnerKey string
		switch key {
		case g.VCPUKey:
			partnerKey = g.RAMKey
		case g.RAMKey:
			partnerKey = g.VCPUKey
		default:
			continue
		}

		var partnerVal float64
		if err := d.conn.QueryRow(`SELECT value FROM assumptions WHERE key = ?`, partnerKey).Scan(&partnerVal); err != nil {
			// No partner stored yet: nothing to validate against.
			continue
		}

		vcpu, ram := value, partnerVal
		if key == g.RAMKey {
			vcpu, ram = partnerVal, value
		}
		if vcpu <= 0 {
			return fmt.Errorf("%s must be greater than zero", g.VCPUKey)
		}
		ratio := ram / vcpu
		if !reg.IsAllowedRatio(ratio) {
			return fmt.Errorf("%g vCPU : %g GiB is 1:%g; allowed ratios are %s",
				vcpu, ram, ratio, reg.FormatAllowed())
		}
	}
	return nil
}
