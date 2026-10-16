# AI Monitor

A glanceable AI dashboard for the NanoKVM Go / Go+ screen:

| Tile | Shows | Source |
|------|-------|--------|
| **Spend** | today / month in USD | Anthropic Admin API cost report, OpenAI organization costs, OpenRouter key usage |
| **Agents** | coding-agent sessions: working / **waiting for you** / idle / done | Claude Code hooks on your machines push check-ins to the KVM |
| **KVM control** | whether an AI is driving *this* KVM right now, with a **kill switch** | a watched MCP endpoint in front of the KVM's own MCP server |
| **Local LLMs** | loaded models, running requests, tokens/s | Ollama, LM Studio, vLLM, llama.cpp |

Tap a tile for details; tap the header to go back. A background service
(`nanokvm-ai-monitor.service`) does the work, so alerts arrive (ntfy, Discord,
Pushover, Home Assistant) even when the App is closed.

## Agent sessions (Claude Code hooks)

The service listens on **https://&lt;tailscale ip&gt;:8790** by default (`AI_BIND`:
`tailscale`, `lan`, `all`, or an IP). The Agents page shows the URL, the
generated **token**, and the certificate fingerprint.

On each machine you run Claude Code on, copy
[`hooks/nanokvm_agent_hook.py`](hooks/nanokvm_agent_hook.py) somewhere (stdlib only), fetch
the KVM's certificate to pin it, and add the hooks to `~/.claude/settings.json`:

```bash
openssl s_client -connect <kvm-ip>:8790 </dev/null 2>/dev/null | openssl x509 > ~/.claude/nanokvm.crt
```

```json
{
  "env": {
    "NANOKVM_AI_URL": "https://100.x.y.z:8790",
    "NANOKVM_AI_TOKEN": "<token from the Agents page>",
    "NANOKVM_AI_CERT": "/Users/you/.claude/nanokvm.crt"
  },
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 ~/.claude/nanokvm_agent_hook.py", "async": true}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/nanokvm_agent_hook.py", "async": true}]}],
    "Notification":     [{"hooks": [{"type": "command", "command": "python3 ~/.claude/nanokvm_agent_hook.py", "async": true}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python3 ~/.claude/nanokvm_agent_hook.py", "async": true}]}],
    "SessionEnd":       [{"hooks": [{"type": "command", "command": "python3 ~/.claude/nanokvm_agent_hook.py", "async": true}]}]
  }
}
```

Compare the fingerprint the Agents page shows with
`openssl x509 -in ~/.claude/nanokvm.crt -outform der | shasum -a 256` (first 16 hex digits).
The hook only sends the machine name, session id, project folder name, state, and Claude
Code's notification text (e.g. "Claude needs your permission to use Bash") — never prompts,
code, or tool input. It times out after 2 s, prints nothing, and always exits 0. `PreToolUse`
/ `PostToolUse` hooks work too (state `working`) but fire on every tool call; they're optional.

When a session starts **waiting** (permission prompt or a question) the tile turns red and,
with `AGENT_NOTIFY=true`, you get an alert. Sessions disappear 10 minutes after they end, or
after 2 hours of silence.

## KVM control and the kill switch

Point AI agents that drive this KVM at the **watched endpoint**
`https://<kvm-ip>:8790/mcp` instead of `https://<kvm-ip>/api/mcp`. It forwards everything
to the KVM's MCP server with the same API key and logs **tool names only** (never arguments
such as typed text). The tile shows **AI ACTIVE** while keyboard/mouse tools ran in the last
30 s, and **watching** for screenshots.

**PAUSE AI CONTROL** blocks every call through the watched endpoint with an MCP error,
immediately (one tap). Resuming takes two taps. Clients that talk to `/api/mcp` directly
bypass the monitor: to enforce it, rotate the MCP key and only give agents the watched URL
over a network you trust (the endpoint is bound to Tailscale by default).

## Spend

| Setting | Key type | Notes |
|---------|----------|-------|
| `ANTHROPIC_ADMIN_KEY` | Admin key (`sk-ant-admin…`) | Console → Settings → Admin keys; organization-wide cost report |
| `OPENAI_ADMIN_KEY` | Organization admin key | `/v1/organization/costs` |
| `OPENROUTER_KEY` | Normal API key | that key's daily/monthly usage and remaining limit |

Spend refreshes every 10 minutes (the providers' reports lag anyway). Admin keys are
powerful: they're stored only in the App's settings on the device (root-only), sent only to
the provider over verified HTTPS, and never shown on screen or written to logs.

## Local model servers

Set any of `OLLAMA_URL`, `LMSTUDIO_URL`, `VLLM_URL`, `LLAMACPP_URL` (polled every 15 s):
Ollama `/api/ps`, LM Studio `/api/v0/models`, vLLM `/metrics`, llama.cpp `/metrics` +
`/health` (start `llama-server` with `--metrics`).

## Resource use

About 19 MB RSS and well under 1% CPU on the Go+. The service is sandboxed
(`ProtectSystem=full`, limited memory and tasks) and removes itself after the App is
uninstalled.
