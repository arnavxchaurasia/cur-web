// Package metrics exposes in-process counters via expvar.
// Accessible at GET /debug/vars (registered automatically by the expvar package).
package metrics

import "expvar"

var (
	JobsSubmitted = expvar.NewInt("jobs_submitted")
	JobsDone      = expvar.NewInt("jobs_done")
	JobsFailed    = expvar.NewInt("jobs_failed")
	JobsRetried   = expvar.NewInt("jobs_retried")
)
