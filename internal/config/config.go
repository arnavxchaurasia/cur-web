package config

import (
	"errors"
	"os"
	"strconv"
	"strings"
)

// Shared defaults referenced across packages so a change here propagates
// everywhere instead of being copy-pasted. See CLAUDE.md for the inventory
// these constants replaced.
const (
	// DefaultAGYModel is the `agy --model` alias used when $AGY_MODEL is unset.
	DefaultAGYModel = "gemini-3.5-flash"
	// AGYTimeoutMinutes bounds both agy's per-phase --print-timeout and the
	// watcher's stale-job timeout. One number so they can never drift apart.
	AGYTimeoutMinutes = 45
	// TotalPhases is the number of pipeline phases (ingest → report).
	TotalPhases = 6
)

// DefaultAllowedDomains is the login allow-list used when $ALLOWED_DOMAINS is
// unset. A var (not const) because Go has no const slices.
var DefaultAllowedDomains = []string{"google.com", "facets.cloud"}

// AGYPrintTimeout renders AGYTimeoutMinutes in agy's duration form ("45m").
func AGYPrintTimeout() string { return strconv.Itoa(AGYTimeoutMinutes) + "m" }

type Config struct {
	Port               string
	DataDir            string
	GoogleClientID     string
	GoogleClientSecret string
	GoogleRedirectURI  string
	SessionSecret      string
	ResendAPIKey       string
	ResendFrom         string
	AppBaseURL         string
	SlackWebhookURL    string
	DevAuthBypass      bool
	// AGYPath is the path to the agy binary. From $AGY_PATH; defaults to
	// "agy" (expects it on PATH). Set this on Windows where the binary lives
	// at a fixed path and is not on PATH.
	AGYPath string
	// AGYModel is the model alias passed to `agy --model`. From
	// $AGY_MODEL, default "gemini-3.5-flash".
	AGYModel string
	// AdminEmails is a comma-separated allow-list from $ADMIN_EMAILS.
	// Members can see every job (not just their own) via /api/admin/*.
	// Empty = no admins. Compared case-insensitively against session.Email.
	AdminEmails []string
	// AllowedDomains is a comma-separated list of Google Workspace domains
	// permitted to log in, from $ALLOWED_DOMAINS. Defaults to
	// "google.com,facets.cloud" if unset.
	AllowedDomains []string
}

func (c *Config) JobsDir() string { return c.DataDir + "/jobs" }
func (c *Config) DBPath() string  { return c.DataDir + "/cur-web.db" }

func LoadFromEnv() (*Config, error) {
	devBypass := os.Getenv("DEV_AUTH_BYPASS") == "true"

	required := []string{"SESSION_SECRET", "APP_BASE_URL"}
	if !devBypass {
		required = append(required,
			"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
			"RESEND_API_KEY", "RESEND_FROM", "SLACK_WEBHOOK_URL",
		)
	}
	// ANTIGRAVITY_API_KEY is intentionally NOT in the required list. It must be
	// set in the host environment (VM-wide on prod, shell on dev) so the
	// spawned agy subprocess inherits it via os.Environ(). GEMINI_API_KEY is
	// ignored by agy — use ANTIGRAVITY_API_KEY.
	for _, k := range required {
		if os.Getenv(k) == "" {
			return nil, errors.New("missing required env var: " + k)
		}
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	model := os.Getenv("AGY_MODEL")
	if model == "" {
		model = DefaultAGYModel
	}
	dataDir := os.Getenv("DATA_DIR")
	if dataDir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			home = "/var/lib/cur-web"
		}
		dataDir = home + "/.cur-web"
	}
	return &Config{
		AGYPath:            os.Getenv("AGY_PATH"),
		AGYModel:           model,
		Port:               port,
		DataDir:            dataDir,
		GoogleClientID:     os.Getenv("GOOGLE_CLIENT_ID"),
		GoogleClientSecret: os.Getenv("GOOGLE_CLIENT_SECRET"),
		GoogleRedirectURI:  os.Getenv("GOOGLE_REDIRECT_URI"),
		SessionSecret:      os.Getenv("SESSION_SECRET"),
		ResendAPIKey:       os.Getenv("RESEND_API_KEY"),
		ResendFrom:         os.Getenv("RESEND_FROM"),
		AppBaseURL:         os.Getenv("APP_BASE_URL"),
		SlackWebhookURL:    os.Getenv("SLACK_WEBHOOK_URL"),
		DevAuthBypass:      devBypass,
		AdminEmails:        parseAdminEmails(os.Getenv("ADMIN_EMAILS")),
		AllowedDomains:     parseAllowedDomains(os.Getenv("ALLOWED_DOMAINS")),
	}, nil
}

func parseAllowedDomains(raw string) []string {
	if raw == "" {
		return DefaultAllowedDomains
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.ToLower(strings.TrimSpace(p))
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func parseAdminEmails(raw string) []string {
	if raw == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.ToLower(strings.TrimSpace(p))
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}
