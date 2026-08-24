// Single source of truth for the pipeline phases in the UI. Mirrors the Go
// config.TotalPhases constant (internal/config/config.go). If the pipeline gains
// or loses a phase, update PHASES here and config.TotalPhases on the Go side.
export const PHASES = [
  { n: 1, name: 'Ingestion', blurb: 'load & classify line items' },
  { n: 2, name: 'Mapping', blurb: 'AWS service → GCP equivalent' },
  { n: 3, name: 'Review', blurb: 'sanity-check the mappings' },
  { n: 4, name: 'Rate fill', blurb: 'on-demand & CUD pricing' },
  { n: 5, name: 'Outlier triage', blurb: 'investigate anomalies' },
  { n: 6, name: 'Report', blurb: 'render the HTML deliverable' },
] as const

export const TOTAL_PHASES = PHASES.length
