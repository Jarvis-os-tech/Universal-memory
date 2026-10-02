# Universal Memory

One shared memory for every agent on this machine — Hermes, OpenCode, Claude
Code, Codex, your own agents, and browser-based assistants over MCP.

```
read / get / append / log / search / list   free, no prompt
delete                                      human approval, always
```

- **Command:** `universal-memory` (`umem` is kept as a legacy alias)
- **MCP server:** `universal` → tools `universal_memory_put` / `_get` /
  `_append` / `_search` / `_list` / `_delete`
- **Store:** `~/.local/share/universal-memory/memory.db` (SQLite WAL + FTS5, `0600`)
- **Config:** `~/.config/universal-memory/config.json`
- No daemon, no third-party dependencies — one Python stdlib script.

## Layout

```
src/universal_memory.py                     the whole implementation
tests/test_universal_memory.py              63 tests
packaging/install.sh                        standalone installer
packaging/universal-memory-mcp.service      systemd user unit for `serve`
docs/integrations.md                        per-agent MCP config
Makefile                                    test / install / serve / web / unit
```

## Install

```sh
make install          # or: ./packaging/install.sh
```

which is just:

```sh
install -m 0755 src/universal_memory.py ~/.local/bin/universal-memory
ln -sf ~/.local/bin/universal-memory ~/.local/bin/umem   # optional legacy alias
universal-memory init
universal-memory whoami       # actor id, paths, namespaces
```

```sh
make test           # python3 tests/test_universal_memory.py -v
make unit           # install + enable the systemd user unit for the MCP endpoint
```

## CLI (local agents)

Targets are `key` or `namespace:key`.

```sh
universal-memory put hermes:stack "voice stack lives in ~/Downloads" --tags voice,ubuntu
universal-memory get hermes:stack
universal-memory get hermes:stack --json    # + type, tags, actor, timestamps, version
universal-memory append hermes:stack "more context"
universal-memory log runs:today "agent finished a run"   # timestamped, append-only
printf 'long value' | universal-memory put hermes:notes --stdin

universal-memory search voice --json        # full-text search across ALL namespaces
universal-memory search voice --ns hermes   # ...scoped
universal-memory list --ns hermes --limit 20
universal-memory list --namespaces
universal-memory list --tag voice

universal-memory del hermes:stack           # interactive: asks you to type 'yes'
```

Exit codes: `0` ok · `1` error · `2` not found · `3` approval required ·
`4` human declined.

## Deleting: the one gated operation

**Interactive (you at a terminal):** `universal-memory del ns:key` shows the
record and asks you to type `yes`.

**Non-interactive (an agent, cron, MCP):** the delete is refused and a
short-lived approval token is minted instead:

```console
$ universal-memory del shared:note
{
  "deleted": false,
  "approval_required": true,
  "token": "JE4V0hnU7jQY",
  "approve_with": "universal-memory approve JE4V0hnU7jQY",
  "expires_at": "2026-09-24T05:58:53+00:00",
  ...
}
# exit code 3 — the record is still there
```

Only a human at a shell redeems it:

```sh
universal-memory approve JE4V0hnU7jQY   # now the record is deleted
universal-memory deny    JE4V0hnU7jQY   # ...or keep it
universal-memory pending                 # list everything waiting on you
```

`approve` / `deny` exist **only** on the CLI — they are deliberately not MCP
tools, so an agent can never approve its own deletion. Tokens expire after 15
minutes (`approval_ttl_seconds`) and can be redeemed once.

Already decided yourself? `universal-memory del ns:key --yes`.

## MCP (browser agents, and any MCP-capable runtime)

### stdio — for local runtimes

```sh
universal-memory mcp
```

OpenCode (already configured globally on this machine):

```sh
opencode mcp add universal --global -- universal-memory mcp
opencode mcp list            # ✓ universal  connected
```

Agents see `universal_memory_put`, `universal_memory_get`,
`universal_memory_append`, `universal_memory_search`, `universal_memory_list`,
`universal_memory_delete`.

Hermes (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  universal:
    command: universal-memory
    args: ["mcp"]
```

Claude Code / Codex / anything else MCP-capable:

```sh
claude mcp add universal -- universal-memory mcp
```

### HTTP — two commands, two ports

They are deliberately separate, because they have different lifetimes: the MCP
endpoint is something you leave running, the dashboard is something you open.

| command | port | mounts | lifetime |
| --- | --- | --- | --- |
| `universal-memory serve` | `8787` | `POST /mcp` only | **continuous** — run it from the systemd unit below |
| `universal-memory web` | `8788` | `/` dashboard + `/api/*` | on demand — start it, look, stop it |

| surface | endpoint | auth |
| --- | --- | --- |
| **MCP** | `POST /mcp` on `8787` | `Authorization: Bearer <token>` |
| **Web dashboard** | `http://127.0.0.1:8788/?t=<token>` | token in the URL (moved into `sessionStorage`, stripped from the address bar) |
| **JSON API** | `/api/*` on `8788` | `X-UMem-Token: <token>` or `Authorization: Bearer <token>` |

Both bind `127.0.0.1` and share the one bearer token from the config.
`GET /health` is unauthenticated on both and reports which surface it is
(`"surfaces": ["mcp"]` vs `["dashboard"]`). Each refuses the other's paths with
a 404 that names the right command, rather than quietly serving them.

Ports come from `http.port` / `http.web_port` in the config and can be
overridden with `--port`. Want both in one process? `serve --dashboard` or
`web --mcp`.

#### Keep the MCP endpoint up continuously

```sh
systemctl --user enable --now universal-memory-mcp
systemctl --user status universal-memory-mcp
journalctl --user -u universal-memory-mcp -f   # the bearer token is printed at start
```

Unit file: `~/.config/systemd/user/universal-memory-mcp.service`. To undo:
`systemctl --user disable --now universal-memory-mcp`.

#### Open the dashboard when you want it

```sh
universal-memory web    # prints http://127.0.0.1:8788/?t=<token>
```

It reads the same database directly (WAL), so it works whether or not the MCP
service is running, and you can stop it as soon as you are done.

#### Dashboard

- **Overview** — record count, namespaces, who is writing, recent writes
- **Records** — full-text search, filter by namespace and by agent, click a row
  for the full value, tags, version, provenance and timestamps
- **Agents** — every known runtime detected on this machine: installed or not,
  version, its config file, whether it is connected to Universal Memory, and a
  **Connect** button (or the exact command to run) plus **View its memory**
- **Shared facts** — the same information stored by more than one agent:
  identical content, the same key, or very similar wording
- **Activity** — the append-only audit trail
- **Approvals** — pending deletions with Approve / Deny; your click is recorded
  in the audit trail as `human:<you>`

#### API

| method | path | notes |
| --- | --- | --- |
| GET | `/api/stats` | totals, namespaces, actors, agents, pending count |
| GET | `/api/namespaces` | |
| GET | `/api/records?ns=&actor=&q=&tag=&limit=` | `q` switches to FTS search |
| GET | `/api/record?ns=&key=` | 404 if absent |
| GET | `/api/search?q=&ns=&limit=` | |
| GET | `/api/agents` | installed / version / connected / manual command |
| POST | `/api/agents/<id>/connect` | verified recipes only; 404 for unknown ids |
| GET | `/api/overlap` | cross-agent duplicate clusters |
| GET | `/api/audit?limit=` | |
| GET | `/api/approvals` | pending deletes |
| POST | `/api/delete-request` `{ns,key}` | mints an approval token, deletes nothing |
| POST | `/api/approve` `{token}` | human consent |
| POST | `/api/deny` `{token}` | |

> Browser-hosted assistants cannot reach `127.0.0.1` on your machine. To give
> one access you must publish the endpoint yourself (e.g. a tunnel) **and**
> treat the bearer token as a secret — anyone who has it can read and write
> every shared memory. `serve --host 0.0.0.0` alone is not enough. The token is
> redacted from server access logs.

## Namespaces

| namespace | owner |
| --- | --- |
| `hermes` | Hermes sessions, cron, kanban workers |
| `opencode` | OpenCode sessions and subagents |
| `claude-code`, `codex`, ... | future runtimes |
| `browser`, `chatgpt`, `grok` | browser-based assistants |
| `shared` | facts intended for everybody |
| `global` | default when no namespace is given |

Anyone may read anything, and anyone may write into any namespace — attribution
is preserved per record (`actor`) and in the append-only `audit` table, so a
bad write is always traceable to whoever made it.

## Identity

The actor recorded on every write is, in order:

1. `$UNIVERSAL_MEMORY_ACTOR` if you set it
2. a runtime marker (`HERMES_SESSION_ID` → `hermes:...`, `CLAUDECODE` →
   `claude-code`, `OPENCODE` → `opencode`, `CODEX_*` → `codex`, ...)
3. `user@hostname`

## Storage

```
memory       ns, key, type(kv|log), value, tags, actor, timestamps, version
memory_fts   FTS5 index over key/value/tags, kept in sync by triggers
audit        who did what, when, to which record
approval     pending / approved / denied / expired delete requests
```

Config keys: `db`, `default_ns`, `max_value_bytes` (1 MB),
`approval_ttl_seconds` (900), `audit`, `http.{host,port,token}`.

Environment overrides: `UNIVERSAL_MEMORY_DB`, `UNIVERSAL_MEMORY_CONFIG`,
`UNIVERSAL_MEMORY_ACTOR` (the older `UMEM_*` names still work).

## Import existing memory

```sh
universal-memory import ~/.hermes/memories/MEMORY.md --ns hermes --split-by '§' --tags imported
```

## Tests

```sh
make test
# or
python3 tests/test_universal_memory.py
```

63 tests covering CRUD, FTS search, tag filters, the approval flow (token
expiry and reuse), both MCP transports, agent detection, cross-agent overlap,
the JSON API (including auth), the `serve`/`web` surface split, and a syntax
check of the dashboard's inline JavaScript via `node --check`.
