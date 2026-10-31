// Package mcp implements a Model Context Protocol (MCP) server over the
// Streamable HTTP transport, co-located with the REST API so it reuses the
// exact same bearer-token auth (auth.TokenMiddleware), per-user session, and
// scope enforcement. The server is a thin adapter: each tool invokes an
// existing HTTP handler through an in-process request, so "who can see what"
// lives in one place (the handler + canAccess), never duplicated here.
//
// Remote clients (Claude Desktop, Praxis) connect with:
//
//	endpoint: ${APP_BASE_URL}/mcp
//	header:   Authorization: Bearer fct_live_...
//
// The whole server is gated by MCP_ENABLED and every tool is individually
// toggleable through the registry, so operators configure at deploy time
// without a rebuild.
package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"

	"github.com/facets/cur-web/internal/auth"
	"github.com/go-chi/chi/v5"
)

// ProtocolVersion is the MCP protocol revision this server speaks.
const ProtocolVersion = "2024-11-05"

// ServerName is advertised in initialize responses so clients show a stable
// identity per deployment.
const ServerName = "facets-cur-web"

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope
// ---------------------------------------------------------------------------

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// JSON-RPC standard error codes.
const (
	codeParseError     = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
	codeInvalidParams  = -32602
	codeInternalError  = -32603
	// Application-defined: the authenticated token lacks the tool's scope.
	codeInsufficientScope = -32000
)

// ---------------------------------------------------------------------------
// Tool registry
// ---------------------------------------------------------------------------

// ToolResult is what a tool handler returns: a block of text content (JSON or
// prose). isError marks tool-level failures (vs transport errors).
type ToolResult struct {
	Text    string
	IsError bool
}

// ToolDef is one registered tool. Adding a future tool is a single append —
// a stable name, a required scope, an enabled predicate, and an invoke func.
type ToolDef struct {
	Name        string
	Description string
	// Scope is the token scope required to call the tool (e.g. db.ScopeJobsRead).
	Scope string
	// InputSchema is the JSON Schema advertised in tools/list.
	InputSchema map[string]any
	// Enabled is evaluated at request time so an operator can toggle a tool
	// without a restart. nil means always enabled.
	Enabled func() bool
	// Invoke runs the tool. ctx carries the authenticated *Session and the
	// token scopes, exactly as a REST handler would see them.
	Invoke func(ctx context.Context, args json.RawMessage) (ToolResult, error)
}

func (t ToolDef) enabled() bool {
	return t.Enabled == nil || t.Enabled()
}

// Registry is the ordered set of tools the server exposes.
type Registry struct {
	tools []ToolDef
}

// NewRegistry builds a registry from the given tools.
func NewRegistry(tools ...ToolDef) *Registry {
	return &Registry{tools: tools}
}

// Add appends a tool. Kept so callers/tests can extend a registry.
func (r *Registry) Add(t ToolDef) { r.tools = append(r.tools, t) }

func (r *Registry) find(name string) (ToolDef, bool) {
	for _, t := range r.tools {
		if t.Name == name {
			return t, true
		}
	}
	return ToolDef{}, false
}

// ---------------------------------------------------------------------------
// HTTP handler (Streamable HTTP transport)
// ---------------------------------------------------------------------------

// Handler returns the http.HandlerFunc serving POST /mcp. It must be mounted
// inside the token-authenticated route group so the request context already
// carries the *Session and token scopes. A request with no session (neither
// cookie nor token) is rejected 401 by the surrounding middleware before it
// reaches here.
func Handler(reg *Registry) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// GET on the MCP endpoint is the SSE stream channel in Streamable HTTP.
		// This server is request/response only (no server-initiated
		// notifications yet), so there is nothing to stream — tell the client.
		if r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusMethodNotAllowed)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "streaming not supported; use POST"})
			return
		}

		var req rpcRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeRPC(w, rpcResponse{JSONRPC: "2.0", Error: &rpcError{codeParseError, "parse error"}})
			return
		}
		if req.JSONRPC != "2.0" || req.Method == "" {
			writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{codeInvalidRequest, "invalid request"}})
			return
		}

		switch req.Method {
		case "initialize":
			writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Result: initializeResult()})
		case "notifications/initialized", "ping":
			// Notifications have no id and expect no body; ping is a no-op.
			writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Result: map[string]any{}})
		case "tools/list":
			writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Result: reg.listResult()})
		case "tools/call":
			reg.handleCall(w, r, req)
		default:
			writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{codeMethodNotFound, "method not found: " + req.Method}})
		}
	}
}

func initializeResult() map[string]any {
	return map[string]any{
		"protocolVersion": ProtocolVersion,
		"capabilities": map[string]any{
			"tools": map[string]any{},
		},
		"serverInfo": map[string]any{
			"name":    ServerName,
			"version": buildVersion(),
		},
	}
}

func (r *Registry) listResult() map[string]any {
	tools := make([]map[string]any, 0, len(r.tools))
	for _, t := range r.tools {
		if !t.enabled() {
			continue
		}
		schema := t.InputSchema
		if schema == nil {
			schema = map[string]any{"type": "object", "properties": map[string]any{}}
		}
		tools = append(tools, map[string]any{
			"name":        t.Name,
			"description": t.Description,
			"inputSchema": schema,
		})
	}
	return map[string]any{"tools": tools}
}

type callParams struct {
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments,omitempty"`
}

func (reg *Registry) handleCall(w http.ResponseWriter, r *http.Request, req rpcRequest) {
	var p callParams
	if err := json.Unmarshal(req.Params, &p); err != nil || p.Name == "" {
		writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{codeInvalidParams, "invalid params"}})
		return
	}
	tool, ok := reg.find(p.Name)
	if !ok || !tool.enabled() {
		writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{codeMethodNotFound, "unknown tool: " + p.Name}})
		return
	}

	// Scope enforcement mirrors auth.RequireScope: a token must carry the
	// tool's scope. (The /mcp route is never cookie-reachable in practice, but
	// if it were, a cookie session is full-privilege and passes.)
	if tool.Scope != "" && auth.TokenAuthenticated(r.Context()) {
		if !hasScope(r.Context(), tool.Scope) {
			writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{codeInsufficientScope, "insufficient scope: " + tool.Scope + " required"}})
			return
		}
	}

	res, err := tool.Invoke(r.Context(), p.Arguments)
	if err != nil {
		writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{codeInternalError, err.Error()}})
		return
	}
	writeRPC(w, rpcResponse{JSONRPC: "2.0", ID: req.ID, Result: map[string]any{
		"content": []map[string]any{{"type": "text", "text": res.Text}},
		"isError": res.IsError,
	}})
}

func hasScope(ctx context.Context, scope string) bool {
	scopes, ok := auth.TokenScopes(ctx)
	if !ok {
		return false
	}
	for _, s := range scopes {
		if s == scope {
			return true
		}
	}
	return false
}

func writeRPC(w http.ResponseWriter, resp rpcResponse) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

// ---------------------------------------------------------------------------
// In-process handler reuse
// ---------------------------------------------------------------------------

// callHandler invokes an existing REST http.HandlerFunc in-process with the
// current authenticated context and any chi URL params, returning the status
// code and response body. This is how MCP tools reuse the REST surface without
// re-implementing authz: the handler runs its own canAccess / owner filtering
// against the same session.
func callHandler(ctx context.Context, h http.HandlerFunc, method, path string, urlParams map[string]string, body []byte) (int, []byte) {
	var reader *bytes.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	} else {
		reader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, reader).WithContext(withChiParams(ctx, urlParams))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h(rec, req)
	return rec.Code, rec.Body.Bytes()
}

func withChiParams(ctx context.Context, params map[string]string) context.Context {
	if len(params) == 0 {
		return ctx
	}
	rctx := chi.NewRouteContext()
	for k, v := range params {
		rctx.URLParams.Add(k, v)
	}
	return context.WithValue(ctx, chi.RouteCtxKey, rctx)
}
