# Setup

Two ways to use this: **installed** (nothing to clone — for Claude Desktop, or for anyone
you share it with) or **from a checkout** (for developing it).

---

## A. Installed — for you and for anyone else

The only prerequisite is [`uv`](https://docs.astral.sh/uv/). Nothing else: no clone, no
Python install, no virtualenv. `uvx` fetches the package from GitHub, builds an isolated
environment and runs it.

### Check it works

```bash
uvx --from git+https://github.com/moritzertl-dev/prodscrape prodscrape paths
```

First run takes ~30s while it builds; after that it is cached and instant. You should see
`home_source  platform user-data dir`.

### Claude Desktop

**Use Settings → Developer → Edit Config.** It opens the correct file for your install,
which is the only reliable way to find it — the location depends on how Desktop was
installed:

| install | config path |
|---|---|
| normal installer | `%APPDATA%\Claude\claude_desktop_config.json` |
| Microsoft Store (MSIX) | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` |

The Store build runs containerised, so `%APPDATA%` is redirected and the obvious path does
not exist at all. If you go looking by hand and find nothing, that is why — not because
Desktop is missing.

**Quit Desktop before editing.** It rewrites this file itself, so a change made while it
is running can be overwritten when it exits.

```json
{
  "mcpServers": {
    "prodscrape": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/moritzertl-dev/prodscrape",
        "prodscrape-mcp"
      ]
    }
  }
}
```

This file also holds all your Desktop preferences, so **merge — do not replace.**
`mcpServers` is a *key inside* the existing top-level object:

```text
{
  "coworkUserFilesPath": "...",
  "preferences": { ... },
  "mcpServers": { "prodscrape": { ... } }
}
```

The easy mistake is pasting the snippet after the closing brace, which produces two
objects side by side and invalid JSON — Desktop then silently ignores the file:

```text
  },
  {                          <- wrong: this starts a second object
  "mcpServers": { ... }
}
}                            <- one brace too many
```

Then **quit Claude Desktop completely** — tray icon → Quit, not just closing the window —
and reopen it. The 14 tools appear under the tools icon in the chat box.

You do not need to run a server, open a port, or start anything at boot. Claude Desktop
launches the process over stdio when it starts and shuts it down when it exits.

### The skill

Desktop does not read `.claude/skills/`. Either:

- **Settings → Capabilities → Skills**, uploading a zip whose root contains `SKILL.md`, or
- paste the contents of [`SKILL.md`](SKILL.md) into a Project's custom instructions.

If neither is available in your build, the MCP tools still work on their own — every tool
carries its own description. The skill only supplies the procedure and the judgement
guidance.

### Where files are written

| situation | location |
|---|---|
| installed (uvx / Desktop) | `%LOCALAPPDATA%\prodscrape` |
| inside a source checkout | `./runs`, `./cache`, `./recipes` |
| `PRODSCRAPE_HOME` set | that directory, always |

To pin a shared install somewhere predictable, add an `env` block to the server config:

```json
{
  "mcpServers": {
    "prodscrape": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/moritzertl-dev/prodscrape", "prodscrape-mcp"],
      "env": { "PRODSCRAPE_HOME": "C:\\Users\\Moritz Ertl\\prodscrape-data" }
    }
  }
}
```

Recipes are searched user-directory-first, then the bundled ones, so a recipe you write
overrides a shipped one without touching the package. The recipes for analytik-jena,
BINDER and QInstruments ship inside the package — a fresh install already knows them.

### Updating

`uvx` caches the build. To pick up new commits:

```bash
uvx --refresh --from git+https://github.com/moritzertl-dev/prodscrape prodscrape paths
```

To pin a colleague to a known-good commit instead of tracking `main`, append `@<sha>` to
the git URL.

---

## B. From a checkout — for development

```bash
git clone https://github.com/moritzertl-dev/prodscrape
cd prodscrape
uv sync
uv run pytest -q        # 96 tests, fully offline
```

Claude Code picks up two committed files automatically:

- `.mcp.json` — registers the MCP server
- `.claude/skills/scrape-product-catalogue/SKILL.md` — the procedure

Restart Claude Code in the directory, approve the server when prompted, and check with
`/mcp`. Invoke the procedure with `/scrape-product-catalogue`, or just ask it to catalogue
a vendor.

In a checkout, artifacts stay in the checkout (`./runs`, `./cache`) so the repo behaves as
it always has.

---

## Troubleshooting

**Tools do not appear in Desktop.** Usually `uvx` is not on the PATH that Desktop sees —
more likely on the Store build, which is containerised. Use the absolute path; find yours
with `where uvx`:

```json
{
  "mcpServers": {
    "prodscrape": {
      "command": "C:\\Users\\moritz ertl\\.local\\bin\\uvx.exe",
      "args": [
        "--from",
        "git+https://github.com/moritzertl-dev/prodscrape",
        "prodscrape-mcp"
      ]
    }
  }
}
```

**First launch is slow.** `uvx` builds the environment the first time (~30s). Desktop may
time out that handshake; quit and reopen once and it will be cached and instant.

**Config looks right but nothing loads.** Validate it — one stray brace makes Desktop
ignore the whole file silently:

```bash
python -c "import json;json.load(open(r'<path to claude_desktop_config.json>'))"
```

**Check the log:** `%APPDATA%\Claude\logs\mcp-server-prodscrape.log`

**JSON errors.** Backslashes must be doubled in JSON, and a trailing comma after the last
entry is invalid.

**Did you fully quit Desktop?** Closing the window leaves it running in the tray; config
is only re-read on a real restart.

---

## Verified

On 2026-09-16, from an empty temp directory with no checkout present:

- `uvx --from git+...` built the package from commit `4ca0627` and installed 39 packages
- `prodscrape paths` resolved `home_source` to the platform user-data dir, not the
  temp directory
- `prodscrape tree analytik-jena.com` discovered 853 URLs and loaded the **bundled**
  recipe (`include=['/products/*'] family_depth=5`)
- `prodscrape-mcp` started and exited cleanly over stdio

Not yet verified: Claude Desktop itself (not installed on this machine), and whether
Desktop supports `/slash` invocation of skills the way Claude Code does.
