"""A browsable view of the device table.

Printing rows into a chat is not a way to read a catalogue: the columns wrap, the spec
bags are unreadable, and nothing can be sorted or searched. This renders the same data
that is in ``devices.csv`` as a self-contained HTML page and opens it.

Self-contained on purpose — no CDN, no network — so it works offline and keeps working
when the page is mailed to someone else.
"""

from __future__ import annotations

import html
import json
import time
import webbrowser
from pathlib import Path

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6b66; --line:#e3e3df;
    --head:#f2f2ef; --accent:#2f5d50; --chip:#eceae4;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#17181a; --fg:#e8e8e4; --muted:#9a9a94; --line:#2c2e31;
             --head:#1f2123; --accent:#7fb8a4; --chip:#26282b; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  header {{ padding:20px 24px 12px; border-bottom:1px solid var(--line); position:sticky;
    top:0; background:var(--bg); z-index:3; }}
  h1 {{ margin:0 0 4px; font-size:18px; font-weight:650; }}
  .meta {{ color:var(--muted); font-size:13px; }}
  .controls {{ margin-top:12px; display:flex; gap:10px; flex-wrap:wrap; }}
  input,select {{ font:inherit; padding:7px 10px; border:1px solid var(--line);
    border-radius:7px; background:var(--bg); color:var(--fg); }}
  input {{ flex:1; min-width:220px; }}
  .wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; }}
  th,td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line);
    vertical-align:top; }}
  th {{ background:var(--head); position:sticky; top:0; cursor:pointer;
    white-space:nowrap; font-weight:600; }}
  th:hover {{ color:var(--accent); }}
  th .ind {{ color:var(--muted); font-weight:400; }}
  td.name {{ font-weight:600; min-width:180px; }}
  td.desc {{ color:var(--muted); max-width:380px; }}
  a {{ color:var(--accent); }}
  .chip {{ display:inline-block; background:var(--chip); border-radius:5px;
    padding:1px 7px; margin:1px 3px 1px 0; font-size:12px; white-space:nowrap; }}
  .specs {{ cursor:pointer; color:var(--accent); white-space:nowrap; }}
  .specrow td {{ background:var(--head); }}
  .specgrid {{ display:grid; grid-template-columns:minmax(160px,auto) 1fr;
    gap:3px 16px; font-size:13px; }}
  .specgrid dt {{ color:var(--muted); }}
  .specgrid dd {{ margin:0; }}
  .hidden {{ display:none; }}
  footer {{ padding:14px 24px 40px; color:var(--muted); font-size:12px; }}
</style></head><body>
<header>
  <h1>{heading}</h1>
  <div class="meta">{count} devices &middot; {categories} categories &middot;
    generated {generated} &middot; source: <code>{csv_path}</code></div>
  <div class="controls">
    <input id="q" placeholder="Filter by name, category, interface or specification…">
    <select id="cat"><option value="">All categories</option>{options}</select>
    <select id="iface"><option value="">Any interface</option>{ifaces}</select>
  </div>
</header>
<div class="wrap"><table id="t">
<thead><tr>{headers}</tr></thead>
<tbody>{rows}</tbody>
</table></div>
<footer>Every value keeps its raw source text. Click a specification count to expand it;
click a column heading to sort. Figures come from <code>devices.csv</code> — this page is
a view of that file, not a separate extract.</footer>
<script>
const rows = [...document.querySelectorAll('tbody tr.dev')];
const q = document.getElementById('q');
const cat = document.getElementById('cat');
const iface = document.getElementById('iface');

function apply() {{
  const needle = q.value.toLowerCase();
  const c = cat.value, i = iface.value;
  for (const tr of rows) {{
    const hay = tr.dataset.hay;
    const ok = (!needle || hay.includes(needle))
      && (!c || tr.dataset.cat === c)
      && (!i || (tr.dataset.iface || '').includes(i));
    tr.classList.toggle('hidden', !ok);
    const sp = tr.nextElementSibling;
    if (sp && sp.classList.contains('specrow')) sp.classList.add('hidden');
  }}
}}
q.addEventListener('input', apply);
cat.addEventListener('change', apply);
iface.addEventListener('change', apply);

document.querySelectorAll('.specs').forEach(el => el.addEventListener('click', () => {{
  const sp = el.closest('tr').nextElementSibling;
  if (sp) sp.classList.toggle('hidden');
}}));

document.querySelectorAll('th').forEach((th, idx) => th.addEventListener('click', () => {{
  const dir = th.dataset.dir === 'asc' ? -1 : 1;
  document.querySelectorAll('th').forEach(o => {{
    o.dataset.dir = ''; const s = o.querySelector('.ind'); if (s) s.textContent = '';
  }});
  th.dataset.dir = dir === 1 ? 'asc' : 'desc';
  const ind = th.querySelector('.ind'); if (ind) ind.textContent = dir === 1 ? ' ▲' : ' ▼';
  const body = document.querySelector('tbody');
  const pairs = rows.map(r => [r, r.nextElementSibling]);
  pairs.sort(([a], [b]) => {{
    const x = a.children[idx].innerText.trim(), y = b.children[idx].innerText.trim();
    const nx = parseFloat(x), ny = parseFloat(y);
    if (!isNaN(nx) && !isNaN(ny)) return (nx - ny) * dir;
    return x.localeCompare(y) * dir;
  }});
  for (const [r, s] of pairs) {{ body.appendChild(r); if (s) body.appendChild(s); }}
}}));
</script></body></html>
"""

COLUMNS = ("name", "category", "interfaces", "description", "specs", "links")


def _chips(value: str) -> str:
    parts = [p.strip() for p in (value or "").split(";") if p.strip()]
    return "".join(f'<span class="chip">{html.escape(p)}</span>' for p in parts) or "—"


def render(records: list[dict], *, domain: str, csv_path: str) -> str:
    """Render extracted device records as a standalone HTML page."""
    # Callers pass exactly the table's rows (pipeline.table_records); filtering here
    # again is how the view once drifted from devices.csv.
    devices = list(records)
    categories = sorted({r["category"] for r in devices if r.get("category")})
    interfaces = sorted({i for r in devices for i in r.get("interfaces", [])})

    body = []
    for index, rec in enumerate(devices):
        specs = {k: (v.get("raw") or "") for k, v in rec["specs"].items()}
        hay = " ".join(
            [rec["name"], rec.get("category", ""), " ".join(rec.get("interfaces", []))]
            + list(specs) + list(specs.values())
        ).lower()

        links = []
        if rec.get("url"):
            links.append(f'<a href="{html.escape(rec["url"])}" target="_blank">page</a>')
        for n, d in enumerate(rec.get("datasheet_urls", [])[:3], 1):
            links.append(f'<a href="{html.escape(d)}" target="_blank">pdf{n}</a>')

        body.append(
            f'<tr class="dev" data-cat="{html.escape(rec.get("category", ""))}" '
            f'data-iface="{html.escape("; ".join(rec.get("interfaces", [])))}" '
            f'data-hay="{html.escape(hay)}">'
            f'<td class="name">{html.escape(rec["name"])}</td>'
            f'<td>{html.escape(rec.get("category", "")) or "—"}</td>'
            f'<td>{_chips("; ".join(rec.get("interfaces", [])))}</td>'
            f'<td class="desc">{html.escape((rec.get("description") or "")[:200])}</td>'
            f'<td class="specs">{len(specs)} specs ▾</td>'
            f'<td>{" · ".join(links) or "—"}</td></tr>'
        )
        grid = "".join(
            f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd>"
            for k, v in sorted(specs.items())
        )
        body.append(
            f'<tr class="specrow hidden"><td colspan="6">'
            f'<dl class="specgrid">{grid}</dl></td></tr>'
        )

    headers = "".join(
        f'<th>{html.escape(c.title())}<span class="ind"></span></th>' for c in COLUMNS
    )
    return _PAGE.format(
        title=f"{domain} — devices",
        heading=f"{domain} — device catalogue",
        count=len(devices),
        categories=len(categories),
        generated=time.strftime("%Y-%m-%d %H:%M"),
        csv_path=html.escape(csv_path),
        options="".join(
            f'<option value="{html.escape(c)}">{html.escape(c)}</option>'
            for c in categories
        ),
        ifaces="".join(
            f'<option value="{html.escape(i)}">{html.escape(i)}</option>'
            for i in interfaces
        ),
        headers=headers,
        rows="".join(body),
    )


def write_and_open(
    records: list[dict], *, domain: str, csv_path: str, out_path: Path, open_it: bool = True
) -> tuple[Path, bool]:
    """Write the view and try to open it in the default browser.

    Returns ``(path, opened)``. Opening can fail in a headless or sandboxed environment;
    that is not an error, the file is still written and the path is returned.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(records, domain=domain, csv_path=csv_path), encoding="utf-8")
    opened = False
    if open_it:
        try:
            opened = webbrowser.open(out_path.resolve().as_uri())
        except Exception:
            opened = False
    return out_path, opened
