package notify

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// EmailConfig is provider-agnostic — currently wired to Resend.
type EmailConfig struct {
	APIKey  string // Resend API key (re_...)
	From    string // From address. Bare email or "Name <email>" form.
	BaseURL string // App base URL — used to build the report-download link.
}

const resendEndpoint = "https://api.resend.com/emails"

func SendJobReady(cfg EmailConfig, toEmail, toName, prospect, jobID string) error {
	subject := fmt.Sprintf("Your GCP cost report for %s is ready", prospect)
	downloadURL := fmt.Sprintf("%s/jobs/%s", cfg.BaseURL, jobID)
	body := fmt.Sprintf(
		"Hi %s,\n\nYour AWS → GCP cost projection for %s is ready.\n\nDownload: %s\n\n— Facets Cloud",
		toName, prospect, downloadURL,
	)
	return sendResend(cfg, toEmail, subject, body)
}

// SendContactInterest emails the admin distribution when a signed-in
// user clicks the landing-page contact CTA. One email per recipient
// (Resend's API treats the `to` array as a single envelope, but we
// loop so a single bad recipient doesn't block the rest).
func SendContactInterest(cfg EmailConfig, toEmails []string, contactName, contactEmail, message string) error {
	if len(toEmails) == 0 {
		return nil
	}
	subject := fmt.Sprintf("Contact request from %s", contactName)
	body := fmt.Sprintf("%s (%s) requested help from the CUR page.\n\n", contactName, contactEmail)
	if message != "" {
		body += fmt.Sprintf("Message:\n%s\n\n", message)
	} else {
		body += "No message attached — the click itself is the signal.\n\n"
	}
	body += "— Facets Cloud"
	var firstErr error
	for _, to := range toEmails {
		if err := sendResend(cfg, to, subject, body); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func SendJobFailed(cfg EmailConfig, toEmail, toName, prospect string) error {
	subject := fmt.Sprintf("Report generation failed for %s", prospect)
	body := fmt.Sprintf(
		"Hi %s,\n\nUnfortunately the report for %s failed to generate. Our team has been notified and will look into it.\n\n— Facets Cloud",
		toName, prospect,
	)
	return sendResend(cfg, toEmail, subject, body)
}

func sendResend(cfg EmailConfig, toEmail, subject, text string) error {
	if cfg.APIKey == "" {
		return fmt.Errorf("resend api key not configured")
	}
	payload, _ := json.Marshal(map[string]any{
		"from":    cfg.From,
		"to":      []string{toEmail},
		"subject": subject,
		"text":    text,
	})
	req, err := http.NewRequest("POST", resendEndpoint, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("resend error: %d %s", resp.StatusCode, string(body))
	}
	return nil
}
