package preflight_test

import (
	"testing"

	"github.com/facets/cur-web/internal/preflight"
	"github.com/stretchr/testify/assert"
)

func TestCheck_AGYMissing(t *testing.T) {
	// In the test environment agy is unlikely to be on PATH and the
	// user skill directory won't have the skill installed, so Check() should
	// return an error. We just verify it doesn't panic and returns something
	// meaningful.
	err := preflight.Check()
	// Either agy not found or skill not found — both are valid failures
	// in a fresh CI environment.
	if err != nil {
		assert.True(t,
			containsAny(err.Error(), "agy not found", "skill not found", "cannot resolve"),
			"unexpected error: %v", err,
		)
	}
}

func containsAny(s string, subs ...string) bool {
	for _, sub := range subs {
		if len(s) >= len(sub) {
			for i := 0; i <= len(s)-len(sub); i++ {
				if s[i:i+len(sub)] == sub {
					return true
				}
			}
		}
	}
	return false
}
