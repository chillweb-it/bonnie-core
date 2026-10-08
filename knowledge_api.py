"""Authenticated read API and stateless MCP adapter for connected agents."""
import hmac
import json
import os
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from knowledge_service import KnowledgeUnavailable, Scope


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    company: str | None = None
    domain: str | None = None
    limit: int = Field(default=6, ge=1, le=20)


def create_api(service):
    app = FastAPI(title="Bonnie Shared Knowledge", docs_url=None, redoc_url=None)

    def authenticate(header):
        supplied = (header or "").removeprefix("Bearer ")
        keys = json.loads(os.getenv("KNOWLEDGE_API_KEYS", "{}"))
        for key, data in keys.items():
            if hmac.compare_digest(supplied, key):
                return Scope(data["principal"],tuple(data.get("companies", [])),tuple(data.get("audience", ["staff"])))
        raise HTTPException(401, "Invalid credentials")

    def search(body, scope):
        try:
            return service.search(body.query, scope, company=body.company, domain=body.domain,limit=body.limit)
        except PermissionError:
            raise HTTPException(403, "Company is outside principal scope")
        except Exception:
            raise HTTPException(503, "Knowledge is unavailable; do not substitute remembered rules")

    @app.get("/health")
    def health():
        return JSONResponse({"service": "bonnie-knowledge", "ready": service.ready.is_set()}, status_code=200 if service.ready.is_set() else 503)

    @app.post("/v1/knowledge/search")
    def knowledge_search(body: SearchRequest, authorization: str | None = Header(default=None)):
        return search(body, authenticate(authorization))

    @app.get("/v1/system/runtime")
    def runtime(authorization: str | None = Header(default=None)):
        scope = authenticate(authorization)
        if "ceo" not in scope.audience:
            raise HTTPException(403, "CEO scope required")
        return {"slack_provider": "anthropic", "slack_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
                "agent_default_model": os.getenv("ANTHROPIC_AGENT_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")),
                "agent_model_override": "Notion registry claude-* overrides default",
                "enable_agent_queue": os.getenv("ENABLE_AGENT_RUN_QUEUE", "true"),
                "knowledge_embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "commit": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown")}

    @app.post("/mcp")
    def mcp(body: dict, authorization: str | None = Header(default=None)):
        scope = authenticate(authorization)
        method, rid = body.get("method"), body.get("id")
        if method == "notifications/initialized":
            return JSONResponse({}, status_code=202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "bonnie-knowledge", "version": "0.6.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "knowledge_search", "description": "Read current approved shared knowledge before company work. Includes complete MUST rules and citations. No results means no approved source, not permission to invent rules.", "inputSchema": SearchRequest.model_json_schema()}]}
        elif method == "tools/call":
            params = body.get("params", {})
            if params.get("name") != "knowledge_search":
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unknown tool"}}
            try:
                data = search(SearchRequest(**params.get("arguments", {})), scope)
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
            except Exception:
                result = {"content": [{"type": "text", "text": "Knowledge unavailable or request outside access scope. Do not invent rules."}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    return app


def start_api(service):
    import threading
    import uvicorn
    threading.Thread(target=uvicorn.run,kwargs={"app": create_api(service), "host": "0.0.0.0", "port": int(os.getenv("PORT", "8080")), "access_log": False},name="knowledge-api",daemon=True).start()
