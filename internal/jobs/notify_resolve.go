package jobs

import (
	"github.com/facets/cur-web/internal/db"
	"github.com/facets/cur-web/internal/notify"
)

// Integration setting keys (stored in the integration_settings table). These
// are the in-app, admin-configurable overrides for the env-based notification
// config. DB value wins; env is the fallback.
const (
	keyEmailEnabled = "email.enabled"
	keyEmailAPIKey  = "email.api_key" // secret
	keyEmailFrom    = "email.from"
	keySlackEnabled = "slack.enabled"
	keySlackWebhook = "slack.webhook_url" // secret
)

// resolveEmailConfig overlays DB-stored email settings on top of the env
// config. Returns an empty-APIKey config when email is disabled, so existing
// "APIKey == ”" guards naturally skip sending.
func resolveEmailConfig(d *db.DB, env notify.EmailConfig) notify.EmailConfig {
	cfg := env
	if d == nil {
		return cfg
	}
	if d.GetIntegrationValueOr(keyEmailEnabled, "true") == "false" {
		cfg.APIKey = ""
		return cfg
	}
	cfg.APIKey = d.GetIntegrationValueOr(keyEmailAPIKey, env.APIKey)
	cfg.From = d.GetIntegrationValueOr(keyEmailFrom, env.From)
	return cfg
}

// resolveSlackURL overlays the DB-stored Slack webhook on the env value.
// Returns "" when Slack is disabled.
func resolveSlackURL(d *db.DB, envURL string) string {
	if d == nil {
		return envURL
	}
	if d.GetIntegrationValueOr(keySlackEnabled, "true") == "false" {
		return ""
	}
	return d.GetIntegrationValueOr(keySlackWebhook, envURL)
}

// Handler resolvers (resolve at send time so admin changes take effect without
// a restart).
func (h *Handler) emailCfg() notify.EmailConfig { return resolveEmailConfig(h.db, h.notify.Email) }
func (h *Handler) slackURL() string             { return resolveSlackURL(h.db, h.notify.SlackURL) }

// Watcher resolvers.
func (w *Watcher) emailCfg() notify.EmailConfig { return resolveEmailConfig(w.db, w.notify.Email) }
func (w *Watcher) slackURL() string             { return resolveSlackURL(w.db, w.notify.SlackURL) }
