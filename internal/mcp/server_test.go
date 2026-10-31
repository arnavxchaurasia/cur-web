package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/facets/cur-web/internal/auth"
	"github.com/facets/cur-web/internal/db"
)

// stubHandler returns a handler that writes a fixed body + status and records
// whether it was called.
func stubHandler(status int, body string, called *bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if called != nil {
			*called = true
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
}

// doRPC sends one JSON-RPC request through the MCP handler with the given
// context (which may carry token auth + scopes) and returns the decoded
// response.
func doRPC(t *testing.T, reg *Registry, ctx context.Context, method string, params any) rpcResponse {
	t.Helper()
	body := map[string]any{"jsonrpc": "2.0", "id": 1, "method": method}
	if params != nil {
		body["params"] = params
	}
	raw, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/mcp", bytes.NewReader(raw)).WithContext(ctx)
	rec := httptest.NewRecorder()
	Handler(reg)(rec, req)
	var resp rpcResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (body=%s)", err, rec.Body.String())
	}
	return resp
}

// ctxWithToken builds a context that looks like a request authenticated by a
// bearer token carrying the given scopes.
func ctxWithToken(scopes ...string) context.Context {
	ctx := context.Background()
	ctx = context.WithValue(ctx, auth.ExportedCtxKey{}, &auth.Session{Email: "u@x.com"})
	return auth.WithTokenAuthForTest(ctx, "key123", scopes)
}

func TestInitialize(t *testing.T) {
	reg := DefaultRegistry(Handlers{})
	resp := doRPC(t, reg, context.Background(), "initialize", nil)
	if resp.Error != nil {
		t.Fatalf("initialize errored: %v", resp.Error)
	}
	m, _ := resp.Result.(map[string]any)
	if m["protocolVersion"] != ProtocolVersion {
		t.Fatalf("protocolVersion = %v, want %s", m["protocolVersion"], ProtocolVersion)
	}
}

func TestToolsListReflectsEnabled(t *testing.T) {
	reg := NewRegistry(
		ToolDef{Name: "on", Scope: db.ScopeJobsRead, Enabled: func() bool { return true },
			Invoke: func(context.Context, json.RawMessage) (ToolResult, error) { return ToolResult{}, nil }},
		ToolDef{Name: "off", Scope: db.ScopeJobsRead, Enabled: func() bool { return false },
			Invoke: func(context.Context, json.RawMessage) (ToolResult, error) { return ToolResult{}, nil }},
	)
	resp := doRPC(t, reg, context.Background(), "tools/list", nil)
	m := resp.Result.(map[string]any)
	tools := m["tools"].([]any)
	if len(tools) != 1 {
		t.Fatalf("expected 1 enabled tool, got %d", len(tools))
	}
	if tools[0].(map[string]any)["name"] != "on" {
		t.Fatalf("wrong tool surfaced: %v", tools[0])
	}
}

func TestToolsCallReusesHandler(t *testing.T) {
	called := false
	h := Handlers{ListJobs: stubHandler(200, `[{"id":"j1"}]`, &called)}
	reg := DefaultRegistry(h)
	resp := doRPC(t, reg, ctxWithToken(db.ScopeJobsRead), "tools/call",
		map[string]any{"name": "list_jobs"})
	if resp.Error != nil {
		t.Fatalf("tools/call errored: %v", resp.Error)
	}
	if !called {
		t.Fatal("underlying REST handler was not invoked")
	}
	content := resp.Result.(map[string]any)["content"].([]any)
	text := content[0].(map[string]any)["text"].(string)
	if text != `[{"id":"j1"}]` {
		t.Fatalf("unexpected tool output: %s", text)
	}
}

func TestToolsCallScopeEnforced(t *testing.T) {
	called := false
	h := Handlers{ListJobs: stubHandler(200, "ok", &called)}
	reg := DefaultRegistry(h)
	// Token lacks jobs:read -> must be rejected before the handler runs.
	resp := doRPC(t, reg, ctxWithToken(db.ScopeReportsRead), "tools/call",
		map[string]any{"name": "list_jobs"})
	if resp.Error == nil || resp.Error.Code != codeInsufficientScope {
		t.Fatalf("expected insufficient-scope error, got %+v", resp.Error)
	}
	if called {
		t.Fatal("handler ran despite missing scope")
	}
}

func TestToolsCallHandlerErrorSurfaces(t *testing.T) {
	h := Handlers{GetJob: stubHandler(403, `{"error":"forbidden"}`, nil)}
	reg := DefaultRegistry(h)
	resp := doRPC(t, reg, ctxWithToken(db.ScopeJobsRead), "tools/call",
		map[string]any{"name": "get_job", "arguments": map[string]any{"id": "other"}})
	if resp.Error != nil {
		t.Fatalf("transport should not error; handler error goes in result: %v", resp.Error)
	}
	res := resp.Result.(map[string]any)
	if res["isError"] != true {
		t.Fatalf("expected isError=true for 403, got %v", res["isError"])
	}
}

func TestUnknownMethod(t *testing.T) {
	reg := DefaultRegistry(Handlers{})
	resp := doRPC(t, reg, context.Background(), "nonsense", nil)
	if resp.Error == nil || resp.Error.Code != codeMethodNotFound {
		t.Fatalf("expected method-not-found, got %+v", resp.Error)
	}
}
