# Wiring agents into Universal Memory

Every runtime needs two things: a way to call the CLI, and — where it speaks
MCP — an MCP server entry pointing at `universal-memory mcp`.

---

## OpenCode

```sh
opencode mcp add universal --global -- universal-memory mcp
```

Writes `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "servers": {
      "universal": { "type": "local", "command": ["universal-memory", "mcp"] }
    }
  }
}
```

Check: `opencode mcp list`

---

## Hermes

Append to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  universal:
    command: universal-memory
    args: ["mcp"]
```

If `mcp_servers:` already exists, add the `universal:` block **underneath** it —
a second top-level `mcp_servers:` key is invalid YAML. Restart Hermes afterwards.

---

## Claude Code

```sh
claude mcp add --scope user universal -- universal-memory mcp
```

---

## Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.universal]
command = "universal-memory"
args = ["mcp"]
```

---

## Cursor / Gemini CLI

Add to `~/.cursor/mcp.json` or `~/.gemini/settings.json`, under `mcpServers`:

```json
{ "universal": { "command": ["universal-memory", "mcp"] } }
```

---

## Aider

Aider has no MCP client. Call the CLI directly from its config, or skip it.

---

## Namespaces

One bucket per agent keeps origins clear while `shared` stays readable by
everyone:

| namespace | written by |
| --- | --- |
| `hermes` | Hermes Agent |
| `opencode` | OpenCode and its subagents |
| `claude-code`, `codex`, `cursor`, `gemini-cli` | those runtimes |
| `browser` | ChatGPT / Grok / Perplexity via the HTTP MCP endpoint |
| `user` | what the agents believe about *you* — preferences, hardware |
| `shared` | no single owner; readable by every agent |
| `global` | the default bucket |

Search crosses every namespace unless you scope it:

```sh
universal-memory search voice              # everywhere
universal-memory search voice --ns hermes  # scoped
```

---

## Browser-based assistants

They cannot reach `127.0.0.1`. Start the endpoint and publish it yourself:

```sh
universal-memory serve                    # 127.0.0.1:8787/mcp, bearer auth
```

Then expose it with a tunnel and give the assistant the URL plus the bearer
token from `~/.config/universal-memory/config.json`. **Treat that token as a
secret** — it grants read and write over every shared memory. Do not bind
`--host 0.0.0.0` without putting authentication in front of it.
