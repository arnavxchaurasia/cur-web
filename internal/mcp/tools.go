package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"runtime/debug"
	"strings"

	"github.com/facets/cur-web/internal/db"
)

// Handlers is the set of existing REST handlers the MCP tools adapt. Pass the
// job handler's methods here from main.go — the MCP layer adds no business
// logic of its own.
type Handlers struct {
	ListJobs http.HandlerFunc // GET /api/jobs        (owner-filtered)
	GetJob   http.HandlerFunc // GET /api/jobs/{id}   (canAccess)
	JobLogs  http.HandlerFunc // GET /api/jobs/{id}/logs
}

// toolEnabled reports whether a tool is enabled, honoring an optional
// per-tool override env var MCP_TOOL_<NAME>_ENABLED (true/false). Absent =
// enabled. This is the deploy-time, no-rebuild toggle.
func toolEnabled(name string) bool {
	env := "MCP_TOOL_" + strings.ToUpper(name) + "_ENABLED"
	if v, ok := os.LookupEnv(env); ok {
		return !strings.EqualFold(strings.TrimSpace(v), "false")
	}
	return true
}

// DefaultRegistry builds the MVP tool set. Every tool is read-only and maps
// 1:1 onto a token-reachable REST route; there are deliberately no admin tools
// because token auth cannot reach admin routes (auth.RequireAdmin rejects it).
func DefaultRegistry(h Handlers) *Registry {
	idSchema := map[string]any{
		"type": "object",
		"properties": map[string]any{
			"id": map[string]any{"type": "string", "description": "Job ID"},
		},
		"required": []string{"id"},
	}
	emptySchema := map[string]any{"type": "object", "properties": map[string]any{}}

	return NewRegistry(
		ToolDef{
			Name:        "list_jobs",
			Description: "List the authenticated user's cost-projection jobs (most recent first).",
			Scope:       db.ScopeJobsRead,
			InputSchema: emptySchema,
			Enabled:     func() bool { return toolEnabled("list_jobs") },
			Invoke: func(ctx context.Context, _ json.RawMessage) (ToolResult, error) {
				return proxy(ctx, h.ListJobs, http.MethodGet, "/api/jobs", nil, nil)
			},
		},
		ToolDef{
			Name:        "get_job",
			Description: "Get one job's detail and status by ID (only if the caller owns it).",
			Scope:       db.ScopeJobsRead,
			InputSchema: idSchema,
			Enabled:     func() bool { return toolEnabled("get_job") },
			Invoke: func(ctx context.Context, args json.RawMessage) (ToolResult, error) {
				id, err := requireID(args)
				if err != nil {
					return ToolResult{Text: err.Error(), IsError: true}, nil
				}
				return proxy(ctx, h.GetJob, http.MethodGet, "/api/jobs/"+id, map[string]string{"id": id}, nil)
			},
		},
		ToolDef{
			Name:        "get_job_logs",
			Description: "Get the run logs / progress for one job by ID.",
			Scope:       db.ScopeJobsRead,
			InputSchema: idSchema,
			Enabled:     func() bool { return toolEnabled("get_job_logs") },
			Invoke: func(ctx context.Context, args json.RawMessage) (ToolResult, error) {
				id, err := requireID(args)
				if err != nil {
					return ToolResult{Text: err.Error(), IsError: true}, nil
				}
				return proxy(ctx, h.JobLogs, http.MethodGet, "/api/jobs/"+id+"/logs", map[string]string{"id": id}, nil)
			},
		},
	)
}

// proxy runs a REST handler in-process and turns its response into a tool
// result. A non-2xx status becomes an error result carrying the body, so the
// agent sees the handler's own error (e.g. 403 not-owner, 404 not-found).
func proxy(ctx context.Context, h http.HandlerFunc, method, path string, params map[string]string, body []byte) (ToolResult, error) {
	if h == nil {
		return ToolResult{Text: "tool handler not configured", IsError: true}, nil
	}
	status, resp := callHandler(ctx, h, method, path, params, body)
	if status < 200 || status >= 300 {
		return ToolResult{Text: fmt.Sprintf("request failed (HTTP %d): %s", status, strings.TrimSpace(string(resp))), IsError: true}, nil
	}
	return ToolResult{Text: string(resp)}, nil
}

func requireID(args json.RawMessage) (string, error) {
	var a struct {
		ID string `json:"id"`
	}
	if len(args) > 0 {
		_ = json.Unmarshal(args, &a)
	}
	if strings.TrimSpace(a.ID) == "" {
		return "", fmt.Errorf("missing required argument: id")
	}
	return a.ID, nil
}

// Enabled reports whether the MCP server should be mounted at all. Default on;
// set MCP_ENABLED=false to disable the whole server at a deployment.
func Enabled() bool {
	if v, ok := os.LookupEnv("MCP_ENABLED"); ok {
		return !strings.EqualFold(strings.TrimSpace(v), "false")
	}
	return true
}

// buildVersion returns the server version from build info, falling back to
// "dev" so a local build still advertises a sensible identity.
func buildVersion() string {
	if info, ok := debug.ReadBuildInfo(); ok {
		if info.Main.Version != "" && info.Main.Version != "(devel)" {
			return info.Main.Version
		}
	}
	return "dev"
}
