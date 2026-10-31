package config

import (
	"encoding/base64"
	"errors"
	"log/slog"
	"os"
	"strconv"
	"strings"
)

// Shared defaults referenced across packages so a change here propagates
// everywhere instead of being copy-pasted. See CLAUDE.md for the inventory
// these constants replaced.
const (
	// DefaultAGYModel is the `agy --model` alias used when $AGY_MODEL is unset.
	DefaultAGYModel = "gemini-3.8-flash"
	// AGYTimeoutMinutes bounds both agy's per-phase --print-timeout and the
	// watcher's stale-job timeout. One number so they can never drift apart.
	AGYTimeoutMinutes = 45
	// TotalPhases is the number of pipeline phases (ingest → report).
	TotalPhases = 6
)

// DefaultAllowedDomains is the login allow-list used when $ALLOWED_DOMAINS is
// unset. A var (not const) because Go has no const slices.
var DefaultAllowedDomains = []string{"facets.cloud"}

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
	// FrontendURL is the base URL of the React frontend (e.g. http://localhost:5173).
	// Used in email links so "View Report" buttons open the frontend, not the API.
	FrontendURL        string
	SlackWebhookURL    string
	DevAuthBypass      bool
	AdminPassword      string
	// AGYPath is the path to the agy binary. From $AGY_PATH; defaults to
	// "agy" (expects it on PATH). Set this on Windows where the binary lives
	// at a fixed path and is not on PATH.
	AGYPath string
	// AGYModel is the model alias passed to `agy --model`. From
	// $AGY_MODEL, default "gemini-3.8-flash".
	AGYModel string
	// AdminEmails is a comma-separated allow-list from $ADMIN_EMAILS.
	// Members can see every job (not just their own) via /api/admin/*.
	// Empty = no admins. Compared case-insensitively against session.Email.
	AdminEmails []string
	// AllowedDomains is a comma-separated list of Google Workspace domains
	// permitted to log in, from $ALLOWED_DOMAINS. Defaults to
	// "facets.cloud" if unset.
	AllowedDomains []string
	// FacetsLoginEmails is a comma-separated allow-list from
	// $FACETS_LOGIN_EMAILS. Only these emails may complete a login where
	// next=/facets; everyone else is rejected before a session is issued.
	// Empty = the /facets login door is closed to everyone.
	FacetsLoginEmails []string

	// DatabaseURL enables connecting to Turso or cloud libSQL instead of local SQLite.
	DatabaseURL string

	// Cloud Object Storage (Cloudflare R2 / AWS S3 / Supabase Storage)
	S3Endpoint  string
	S3Bucket    string
	S3AccessKey string
	S3SecretKey string
	S3Region    string
}

func (c *Config) JobsDir() string     { return c.DataDir + "/jobs" }
func (c *Config) DBPath() string      { return c.DataDir + "/cur-web.db" }
func (c *Config) StopAllPath() string { return c.DataDir + "/STOP_ALL" }

// OAuth credentials come from the environment only. There is deliberately no
// built-in fallback: a credential compiled into the binary is published to
// everyone who can read the repository or the image, and a secret-scanner
// cannot flag what it cannot see as a literal. GOOGLE_CLIENT_ID and
// GOOGLE_CLIENT_SECRET are in the required list below, so a missing value
// fails startup with a named error rather than silently signing in against
// somebody else's OAuth app.
func LoadFromEnv() (*Config, error) {
	if os.Getenv("GOOGLE_REDIRECT_URI") == "" {
		if renderURL := os.Getenv("RENDER_EXTERNAL_URL"); renderURL != "" {
			_ = os.Setenv("GOOGLE_REDIRECT_URI", renderURL+"/login")
		} else {
			_ = os.Setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/api/auth/callback")
		}
	}

	devBypass := os.Getenv("DEV_AUTH_BYPASS") == "true"
	if devBypass {
		if os.Getenv("SESSION_SECRET") == "" {
			_ = os.Setenv("SESSION_SECRET", "cur-web-default-session-secret-key-at-least-32-bytes-long")
		}
		if os.Getenv("APP_BASE_URL") == "" {
			if renderURL := os.Getenv("RENDER_EXTERNAL_URL"); renderURL != "" {
				_ = os.Setenv("APP_BASE_URL", renderURL)
			} else {
				_ = os.Setenv("APP_BASE_URL", "http://localhost:8080")
			}
		}
	}

	// ANTIGRAVITY_API_KEY is intentionally NOT in the required list. It must be
	// set in the host environment (VM-wide on prod, shell on dev) so the
	// spawned agy subprocess inherits it via os.Environ(). GEMINI_API_KEY is
	// ignored by agy — use ANTIGRAVITY_API_KEY.
	required := []string{"SESSION_SECRET", "APP_BASE_URL"}
	if !devBypass {
		required = append(required,
			"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
		)
		// Notification vars are optional locally; warn but don't block startup.
		var missingEmail []string
		if os.Getenv("RESEND_API_KEY") == "" {
			missingEmail = append(missingEmail, "RESEND_API_KEY")
		}
		if os.Getenv("RESEND_FROM") == "" {
			missingEmail = append(missingEmail, "RESEND_FROM")
		}
		if len(missingEmail) > 0 {
			slog.Warn("email notifications disabled (" + strings.Join(missingEmail, ", ") + " unset)")
		}

		if os.Getenv("SLACK_WEBHOOK_URL") == "" {
			slog.Warn("Slack notifications disabled (SLACK_WEBHOOK_URL unset)")
		}
	}
	// A wildcard allow-list lets any Google account sign in, so it requires
	// explicit opt-in. Decide this on the EFFECTIVE list, not the raw string:
	// testing os.Getenv(...) == "*" only catches someone typing the wildcard,
	// and misses every other route to the same state.
	effectiveDomains := parseAllowedDomains(os.Getenv("ALLOWED_DOMAINS"))
	wildcardDomains := false
	for _, d := range effectiveDomains {
		if d == "*" {
			wildcardDomains = true
			break
		}
	}
	if wildcardDomains {
		if os.Getenv("ALLOW_ANY_DOMAIN") != "true" {
			return nil, errors.New("ALLOWED_DOMAINS=* requires explicit ALLOW_ANY_DOMAIN=true — refusing to allow all Google accounts")
		}
		slog.Warn("ALLOWED_DOMAINS=* is active — any Google account can authenticate")
	}
	// SESSION_SECRET must be valid base64 (>=32 bytes decoded) unless dev bypass.
	if !devBypass {
		decoded, err := base64.StdEncoding.DecodeString(os.Getenv("SESSION_SECRET"))
		if err != nil {
			return nil, errors.New("SESSION_SECRET is not valid base64 — refusing to start without a valid session key")
		}
		if len(decoded) < 32 {
			return nil, errors.New("SESSION_SECRET decodes to fewer than 32 bytes — refusing to start without a strong session key")
		}
	}
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

	dbURL := os.Getenv("DATABASE_URL")
	if dbURL == "" {
		dbURL = os.Getenv("TURSO_DATABASE_URL")
	}

	s3Endpoint := os.Getenv("S3_ENDPOINT")
	if s3Endpoint == "" {
		s3Endpoint = os.Getenv("R2_ENDPOINT")
	}

	s3Bucket := os.Getenv("S3_BUCKET")
	if s3Bucket == "" {
		s3Bucket = os.Getenv("R2_BUCKET")
	}

	s3AccessKey := os.Getenv("S3_ACCESS_KEY_ID")
	if s3AccessKey == "" {
		s3AccessKey = os.Getenv("R2_ACCESS_KEY_ID")
	}
	if s3AccessKey == "" {
		s3AccessKey = os.Getenv("AWS_ACCESS_KEY_ID")
	}

	s3SecretKey := os.Getenv("S3_SECRET_ACCESS_KEY")
	if s3SecretKey == "" {
		s3SecretKey = os.Getenv("R2_SECRET_ACCESS_KEY")
	}
	if s3SecretKey == "" {
		s3SecretKey = os.Getenv("AWS_SECRET_ACCESS_KEY")
	}

	s3Region := os.Getenv("S3_REGION")
	if s3Region == "" {
		s3Region = os.Getenv("R2_REGION")
	}
	if s3Region == "" {
		s3Region = "auto"
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
		FrontendURL:        os.Getenv("FRONTEND_URL"),
		SlackWebhookURL:    os.Getenv("SLACK_WEBHOOK_URL"),
		DevAuthBypass:      devBypass,
		AdminPassword:      os.Getenv("ADMIN_PASSWORD"),
		AdminEmails:        parseAdminEmails(os.Getenv("ADMIN_EMAILS")),
		AllowedDomains:     parseAllowedDomains(os.Getenv("ALLOWED_DOMAINS")),
		FacetsLoginEmails:  parseAdminEmails(os.Getenv("FACETS_LOGIN_EMAILS")),
		DatabaseURL:        dbURL,
		S3Endpoint:         s3Endpoint,
		S3Bucket:           s3Bucket,
		S3AccessKey:        s3AccessKey,
		S3SecretKey:        s3SecretKey,
		S3Region:           s3Region,
	}, nil
}

func parseAllowedDomains(raw string) []string {
	// An unset variable means "nobody configured this", which must fall back to
	// the restrictive default — NOT to "*". Returning the wildcard here made an
	// unset ALLOWED_DOMAINS indistinguishable from an explicit, deliberate
	// wildcard, and auth skips the domain check entirely when "*" is present
	// (internal/auth/oauth.go:439). Production ran this way: the variable was
	// never set, so any Google account could sign in, and the ALLOW_ANY_DOMAIN
	// opt-in below never fired because it only ever saw an empty raw string.
	if strings.TrimSpace(raw) == "" {
		return append([]string(nil), DefaultAllowedDomains...)
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
