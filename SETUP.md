# Setup

## Claude Code (works in this repo already)

Two files are committed and picked up automatically when you open this project:

- `.mcp.json` — registers the `prodscrape` MCP server (13 tools)
- `.claude/skills/scrape-product-catalogue/SKILL.md` — the procedure

Restart Claude Code in this directory and approve the server when prompted. Check it with
`/mcp`; invoke the procedure with `/scrape-product-catalogue` or just by asking to
catalogue a vendor.

## Claude Desktop

Claude Desktop reads one config file:

```
%APPDATA%\Claude\claude_desktop_config.json
```

(that expands to `C:\Users\Moritz Ertl\AppData\Roaming\Claude\claude_desktop_config.json`)

Create it if it does not exist, or **merge** the `prodscrape` entry into the existing
`mcpServers` object if it does — do not overwrite the file, or you will drop your other
servers.

```json
{
  "mcpServers": {
    "prodscrape": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\Moritz Ertl\\Claude Unitelabs\\scrape_products_lists",
        "run",
        "prodscrape-mcp"
      ]
    }
  }
}
```

Then quit Claude Desktop completely (tray icon → Quit, not just the window) and reopen it.
The tools appear under the tools icon in the chat box.

Notes:

- `--directory` sets the working directory, so `runs/` and `cache/` are written inside the
  project rather than wherever Desktop was launched from. Verified.
- Backslashes must be doubled in JSON.
- If the server does not appear, `uv` is probably not on the PATH that Desktop sees.
  Replace `"command": "uv"` with the absolute path:
  `"command": "C:\\Users\\moritz ertl\\.local\\bin\\uv.exe"`
- Logs: `%APPDATA%\Claude\logs\mcp-server-prodscrape.log`

### The skill in Desktop

Desktop does not read `.claude/skills/`. Add it under
**Settings → Capabilities → Skills**, uploading a zip whose root contains `SKILL.md`:

```bash
cd .claude/skills/scrape-product-catalogue && zip -r ../../../scrape-product-catalogue.zip SKILL.md
```

If that menu is not present in your build, the MCP tools still work on their own — the
skill only supplies the procedure, and each tool carries its own description. You can also
paste `SKILL.md` into a Project's custom instructions to the same effect.

## Verify

```bash
uv run pytest -q                    # 92 tests, fully offline
uv run prodscrape tree qinstruments.com
```
