package config_test

import (
	"os"
	"testing"

	"github.com/facets/cur-web/internal/config"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestLoadFromEnv(t *testing.T) {
	os.Setenv("PORT", "9090")
	os.Setenv("DATA_DIR", "/tmp/test-data")
	os.Setenv("GOOGLE_CLIENT_ID", "client-id")
	os.Setenv("GOOGLE_CLIENT_SECRET", "client-secret")
	os.Setenv("GOOGLE_REDIRECT_URI", "http://localhost/callback")
	os.Setenv("SESSION_SECRET", "aabbccddeeff00112233445566778899")
	os.Setenv("RESEND_API_KEY", "re_test")
	os.Setenv("RESEND_FROM", "test@example.com")
	os.Setenv("APP_BASE_URL", "http://localhost:9090")
	os.Setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

	cfg, err := config.LoadFromEnv()
	require.NoError(t, err)
	assert.Equal(t, "9090", cfg.Port)
	assert.Equal(t, "/tmp/test-data", cfg.DataDir)
}

func TestLoadFromEnv_MissingRequired(t *testing.T) {
	os.Clearenv()
	_, err := config.LoadFromEnv()
	assert.Error(t, err)
}
