package notify

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

// PostJobSubmitted pings Slack the moment a CUR has been accepted by
// the API — before the AI agent runs. Mirrors the shape of
// PostJobSuccess / PostJobFailed so the channel reads as a continuous
// per-job timeline (submitted → success/failed).
func PostJobSubmitted(webhookURL, prospect, repEmail, jobID string) error {
	return postSlack(webhookURL, fmt.Sprintf(
		"📥 New submission — *%s* · rep: %s · job: `%s`",
		prospect, repEmail, jobID,
	))
}

func PostJobSuccess(webhookURL, prospect, repEmail, jobID string, spend float64) error {
	spendStr := "unknown"
	if spend > 0 {
		spendStr = fmt.Sprintf("$%.0f/mo", spend)
	}
	return postSlack(webhookURL, fmt.Sprintf(
		"✅ New report ready — *%s* · AWS spend: %s · rep: %s · job: `%s`",
		prospect, spendStr, repEmail, jobID,
	))
}

func PostJobFailed(webhookURL, prospect, repEmail, jobID string) error {
	return postSlack(webhookURL, fmt.Sprintf(
		"❌ Job failed — *%s* · rep: %s · job: `%s`",
		prospect, repEmail, jobID,
	))
}

// PostContactInterest pings the same Slack webhook used for job
// notifications when a signed-in user clicks the landing-page contact
// CTA. Message is optional — empty means the click itself is the
// signal.
func PostContactInterest(webhookURL, name, email, message string) error {
	text := fmt.Sprintf("📩 Contact request — *%s* (%s)", name, email)
	if message != "" {
		// Prefix every line with > so multi-line messages render as a block
		// quote in Slack, not just the first line.
		quoted := "> " + strings.ReplaceAll(message, "\n", "\n> ")
		text += "\n" + quoted
	}
	return postSlack(webhookURL, text)
}

func postSlack(webhookURL, text string) error {
	payload, _ := json.Marshal(map[string]string{"text": text})
	resp, err := http.Post(webhookURL, "application/json", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("slack webhook error: %d", resp.StatusCode)
	}
	return nil
}
