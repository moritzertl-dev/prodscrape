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

Edit (or create) this file:

```
%APPDATA%\Claude\claude_desktop_config.json
```

which expands to `C:\Users\<you>\AppData\Roaming\Claude\claude_desktop_config.json`.

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

If the file already exists, **merge** the `prodscrape` entry into the existing
`mcpServers` object — do not replace the file, or you will drop your other servers.

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

**Tools do not appear in Desktop.** Almost always `uv` is not on the PATH that Desktop
sees. Use the absolute path:

```json
"command": "C:\\Users\\moritz ertl\\.local\\bin\\uv.exe",
"args": ["tool", "run", "--from", "git+https://github.com/moritzertl-dev/prodscrape", "prodscrape-mcp"]
```

(`uv tool run` is the same thing as `uvx`.)

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
