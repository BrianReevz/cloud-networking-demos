# Multi-Agent Hybrid OAuth (2-Legged Sentinel & 3-Legged Copilot) Demo

This demo extends [`demos/agent-gateway`](../agent-gateway) in `cloud-networking-solutions` to showcase **identity separation between autonomous 2-Legged OAuth (M2M) background agents and interactive 3-Legged OAuth (user-delegated) Copilots** in Gemini Enterprise / Vertex AI Agent Engine (`gemini-enterprise-demo-508317` / `brianreevz.altostrat.com`).

## Architecture & Components

```text
demos/hybrid-oauth-agentic-demo/
├── bigredreeves-service/       # Component A: Remote Vendor Platform (bigredreeves.com, Port 8001)
├── internal-erp-service/       # Component B: Internal Quote & OIDC Identity Service (brianreevz.altostrat.com, Port 8002)
├── sentinel-agent/             # Component C: Headless 2-Legged OAuth Background Poller & Dispatcher
├── mcp-server/                 # Component D: FastMCP Server for Gemini (stdio + HTTP, Port 8003)
├── web-ui/                     # Component E: Gemini Enterprise Copilot UI & Secure BFF (Port 8004)
├── verify_security_posture.py  # Automated Security & RBAC Verification Suite
├── docker-compose.yml          # Full Multi-Container Orchestration
└── DEMO_SCRIPT.md              # Step-by-Step Live Presenter Script
```

### How This Extends `demos/agent-gateway`
In [`demos/agent-gateway`](../agent-gateway), an ADK agent on Vertex AI Agent Runtime communicates with Cloud Run MCP servers through Agent Gateway using a single workload identity. This demo extends that reference architecture with **dual-credential orchestration**:
1. **2-Legged OAuth (`client_credentials`) for Background Sentinels (`sentinel-agent`)**: Polls external vendor APIs (`bigredreeves.com/api/v1/promotions/active`) with scoped read-only tokens (`catalog.read promotions.read`) and triggers notifications without having permission to mutate customer quotes (`403 Forbidden` on `PATCH /api/v1/quotes/{id}`).
2. **3-Legged OAuth (`authorization_code`) for Human-in-the-Loop Copilots (`web-ui` -> `mcp-server` -> `internal-erp-service`)**: Executes `list_my_active_quotes`, `get_promo_details`, and `reprice_quote` using Brian's delegated token (`sub: brian@brianreevz.altostrat.com`, `scope: quotes:read quotes:write`), enforcing strict row-level tenant isolation (`403 Forbidden` on Dave's Quote `#9999`) and recording `updated_by: brian@brianreevz.altostrat.com` in the audit trail.

## Quick Start

### Option 1: Docker Compose
```bash
sudo sh -c 'echo "127.0.0.1 bigredreeves.com brianreevz.altostrat.com" >> /etc/hosts'
docker compose up --build -d
```

### Option 2: Local Python / `uv` Execution
```bash
uv sync
uv run python bigredreeves-service/main.py &
uv run python internal-erp-service/main.py &
uv run python mcp-server/server.py &
uv run python sentinel-agent/worker.py &
PORT=8004 uv run python web-ui/main.py &
```

### Run the Security Posture Verification Suite
```bash
uv run python verify_security_posture.py
```

### Demo URLs
- **Vendor Demo Control UI (`bigredreeves.com`):** `http://bigredreeves.com:8004/demo-admin` *(or `http://127.0.0.1:8001/demo-admin`)*
- **Gemini Enterprise Copilot UI (`brianreevz.altostrat.com`):** `http://brianreevz.altostrat.com:8004/copilot` *(or `http://127.0.0.1:8004/copilot`)*
