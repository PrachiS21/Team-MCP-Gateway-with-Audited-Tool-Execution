# ADK agent on the MCP gateway

A [Google ADK](https://github.com/google/adk-python) agent whose **only tools are the gateway's**. It connects to the
gateway's `/mcp` endpoint with its own member key, so every call goes through the gateway's key check, routing and
audit log, under the agent's own user.

```
you --adk web--> LlmAgent --McpToolset, Bearer <member key>--> gateway /mcp --> woo-mcp, gst, ...
                    |
                    +-- Groq (gpt-oss-120b) through LiteLLM
```

- **Reads run at once; changes ask you first.** Tools the upstream marks read-only run directly. Every other tool,
  including one that says nothing, waits for your yes (ADK's tool confirmation). A "no" means it never runs.
- **The gateway stays in charge.** The agent's user is a member. Revoking its key in the gateway stops the agent at
  once: ADK then offers the model no tools at all.
- **Separate dependencies.** ADK and LiteLLM live only in this folder (its own `pyproject.toml` and `uv.lock`); they
  never enter the gateway's image.

The code: `gateway_agent/factory.py` (the agent), `gateway_agent/agent.py` (what `adk web` loads).

## Run it
```bash
cd adk-agent
uv sync
```

**1. A key for the agent.** It needs its own member key, never the admin's.
- **Local or Kubernetes gateway:** an admin creates the member `adk-agent@example.com` in the dashboard and issues it a key. With the CLI instead: `gateway-admin create-user adk-agent@example.com` and then `gateway-admin issue-key adk-agent@example.com --name adk`. The key is shown once.
- **Cloud Run gateway:** the key is `ADK_AGENT_KEY` in the `gateway-env` secret, and `gcp-deploy.sh` seeds the user, so the key survives restarts:
  `gcloud secrets versions access latest --secret=gateway-env | sed -n 's/^ADK_AGENT_KEY=//p'`

**2. Settings.** Put them in `gateway_agent/.env`, which git ignores; `adk web` reads it, and variables already set in your shell win. Make it readable only by you (`chmod 600`).
```
GATEWAY_MCP_URL=https://<gateway>/mcp     # default http://localhost:8000/mcp
GATEWAY_API_KEY=wmcp_...                  # the agent's member key
GROQ_API_KEY=gsk_...                      # LiteLLM reads it for groq/... models
# ADK_MODEL=groq/openai/gpt-oss-20b       # default groq/openai/gpt-oss-120b
```

**3. Start it.**
```bash
uv run adk web --port 8090 .     # chat in the browser at http://127.0.0.1:8090 (only this machine can open it)
uv run adk run gateway_agent     # or in the terminal
```
Ask "which orders are open?" and the agent reads at once. Ask "delete product 12" and it asks you before anything happens.

## Security notes
- **The key** is never written to a log or an error message, and the tests check this.
- **LiteLLM is pinned to an exact version.** Versions 1.82.7 and 1.82.8 on PyPI were a credential stealer (March 2026, advisory GHSA-5mg7-485q-xm76). `uv.lock` records every file's hash, so nothing changes until we upgrade on purpose. Before upgrading, check the advisories, and stay inside google-adk's own range for LiteLLM.
- **Tool results are data.** The agent's instruction tells the model never to follow instructions found inside them (the same rule as the dashboard's chat).

## Tests
```bash
uv run pytest
```
No network and no Groq. A real gateway (the backend package, run in-process) stands in front of a small shop with one
read-only tool and one that changes something, and a scripted model plays fixed turns. The tests check:
- the model is offered exactly the gateway's tools;
- a read is audited under the agent's own user;
- a change runs only after a yes, and never after a no;
- revoking the key stops the agent;
- `adk web` finds the agent the way it loads any agent.
