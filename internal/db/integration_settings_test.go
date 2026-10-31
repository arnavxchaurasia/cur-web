package db_test

import (
	"encoding/base64"
	"path/filepath"
	"testing"

	"github.com/facets/cur-web/internal/db"
)

func testKey(t *testing.T) {
	t.Helper()
	// 32 zero bytes is fine for a test key.
	t.Setenv("SETTINGS_ENCRYPTION_KEY", base64.StdEncoding.EncodeToString(make([]byte, 32)))
}

func openTestDB(t *testing.T) *db.DB {
	t.Helper()
	d, err := db.Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = d.Close() })
	return d
}

func TestSecretRoundTripAndMasking(t *testing.T) {
	testKey(t)
	d := openTestDB(t)

	if err := d.SetIntegrationSetting("slack.webhook_url", "https://hooks.slack.com/services/XXXX/secret1234", true, "admin@x.com"); err != nil {
		t.Fatalf("set secret: %v", err)
	}

	// Server-side read decrypts.
	got, err := d.GetIntegrationValue("slack.webhook_url")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got != "https://hooks.slack.com/services/XXXX/secret1234" {
		t.Fatalf("decrypt mismatch: %q", got)
	}

	// Masked list never exposes the plaintext; only configured + last4.
	list, err := d.ListIntegrationSettings()
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	s := list["slack.webhook_url"]
	if !s.Configured || s.Value != "" || s.Last4 != "1234" {
		t.Fatalf("masking wrong: %+v", s)
	}
}

func TestNonSecretVisibleAndFallback(t *testing.T) {
	testKey(t)
	d := openTestDB(t)

	if err := d.SetIntegrationSetting("email.from", "Facets <no-reply@facets.cloud>", false, "admin@x.com"); err != nil {
		t.Fatalf("set: %v", err)
	}
	if v := d.GetIntegrationValueOr("email.from", "env-default"); v != "Facets <no-reply@facets.cloud>" {
		t.Fatalf("DB value should win: %q", v)
	}
	// Absent key falls back to env.
	if v := d.GetIntegrationValueOr("email.api_key", "env-key"); v != "env-key" {
		t.Fatalf("fallback failed: %q", v)
	}
}

func TestSecretRefusedWithoutKey(t *testing.T) {
	t.Setenv("SETTINGS_ENCRYPTION_KEY", "")
	d := openTestDB(t)
	if err := d.SetIntegrationSetting("email.api_key", "re_live_xxx", true, "admin@x.com"); err != db.ErrNoEncryptionKey {
		t.Fatalf("expected ErrNoEncryptionKey, got %v", err)
	}
	if db.EncryptionAvailable() {
		t.Fatal("EncryptionAvailable should be false without a key")
	}
}
