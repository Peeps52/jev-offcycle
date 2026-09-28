"""Write results as a page you look at, not a wall of terminal text.

A shortlist is something you read, sort and click through. A terminal gives
you none of that: no links you can open, no ordering, and it scrolls away.
This writes a self-contained HTML file and opens it.
"""

from __future__ import annotations

import html
import json
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path

CSS = """
:root{--bg:#FBFBFA;--card:#fff;--line:#EAEAEA;--ink:#2F3437;--mute:#787774;
--good:#346538;--goodbg:#EDF3EC;--warn:#956400;--warnbg:#FBF3DB;--bad:#9F2F2D;--badbg:#FDEBEC;
--sans:'SF Pro Display','Helvetica Neue',-apple-system,system-ui,sans-serif;
--mono:'SF Mono',Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 var(--sans);
font-variant-numeric:tabular-nums}
.wrap{max-width:1080px;margin:0 auto;padding:48px 28px 80px}
h1{font:400 34px/1.15 Georgia,serif;letter-spacing:-.02em;margin:0 0 6px}
.sub{color:var(--mute);font-size:14px;margin:0 0 32px}
.bar{display:flex;gap:28px;flex-wrap:wrap;padding:16px 0;border-top:1px solid var(--line);
border-bottom:1px solid var(--line);margin-bottom:28px}
.stat .n{font:500 24px/1 var(--mono);letter-spacing:-.02em}
.stat .l{font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--mute);margin-top:6px}
.tabs{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}
.tab{font:500 13px var(--sans);padding:8px 15px;border:1px solid var(--line);border-radius:999px;
background:var(--card);cursor:pointer;color:var(--ink)}
.tab[aria-selected=true]{background:#111;color:#fff;border-color:#111}
.row{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;
margin-bottom:12px}
.row h2{font:500 17px/1.4 var(--sans);margin:0 0 4px}
.row h2 a{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}
.row h2 a:hover{border-color:var(--ink)}
.org{color:var(--mute);font-size:13.5px;margin-bottom:12px}
.pills{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px}
.p{font:500 10.5px var(--sans);letter-spacing:.05em;text-transform:uppercase;
padding:3px 9px;border-radius:999px}
.p.s{background:var(--goodbg);color:var(--good)}
.p.r{background:var(--warnbg);color:var(--warn)}
.p.x{background:var(--badbg);color:var(--bad)}
.p.n{background:#F1F1F0;color:#333}
.why{font-size:13.5px;color:var(--mute);line-height:1.6}
.sig{margin-top:12px;font:12px var(--mono);color:var(--mute)}
details summary{cursor:pointer;font-size:12.5px;color:var(--mute);margin-top:10px}
pre{font:11.5px var(--mono);background:#F7F7F6;padding:12px;border-radius:8px;overflow-x:auto}
footer{margin-top:44px;padding-top:22px;border-top:1px solid var(--line);
font-size:12.5px;color:var(--mute);line-height:1.7;max-width:70ch}
"""

JS = """
const tabs=[...document.querySelectorAll('.tab')];
function show(v){
  tabs.forEach(t=>t.setAttribute('aria-selected',String(t.dataset.v===v)));
  document.querySelectorAll('.row').forEach(r=>{
    r.style.display=(v==='all'||r.dataset.verdict===v)?'':'none';});
}
tabs.forEach(t=>t.addEventListener('click',()=>show(t.dataset.v)));
show('shortlist');
if(!document.querySelector('.row[data-verdict=shortlist]')) show('review');
if(!document.querySelector('.row[data-verdict=review]')
   && !document.querySelector('.row[data-verdict=shortlist]')) show('all');
"""


def write(results, path: Path, meta: dict | None = None, open_it: bool = True) -> Path:
    meta = meta or {}
    e = html.escape
    counts = {v: sum(1 for r in results if r.verdict == v)
              for v in ("shortlist", "review", "reject")}
    cost = sum(r.cost_usd for r in results)

    rows = []
    for r in sorted(results, key=lambda r: r.score, reverse=True):
        cls = {"shortlist": "s", "review": "r", "reject": "x"}[r.verdict]
        title = e(r.listing.title or "Untitled")
        link = (f'<a href="{e(r.listing.url)}" target="_blank" rel="noopener">{title}</a>'
                if r.listing.url else title)
        cand = (f'<span class="p n">candidacy {r.candidacy:.2f}</span>'
                if r.candidacy is not None else "")
        loc = f" · {e(r.listing.location)}" if r.listing.location else ""
        rows.append(f"""
<div class="row" data-verdict="{r.verdict}">
  <h2>{link}</h2>
  <div class="org">{e(r.listing.organisation)}{loc}</div>
  <div class="pills">
    <span class="p {cls}">{r.verdict}</span>
    <span class="p n">fit {r.score:.2f}</span>{cand}
  </div>
  <div class="why">{e('; '.join(r.reasons)) or '—'}</div>
  <details><summary>signals</summary><pre>{e(json.dumps(r.signals, indent=2))}</pre></details>
</div>""")

    doc = f"""<!doctype html><meta charset="utf-8">
<title>Shortlist — {datetime.now():%d %b %Y}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style>
<div class="wrap">
<h1>Shortlist</h1>
<p class="sub">{datetime.now():%d %B %Y, %H:%M} · {e(str(meta.get('source','')))}</p>
<div class="bar">
  <div class="stat"><div class="n">{counts['shortlist']}</div><div class="l">Shortlist</div></div>
  <div class="stat"><div class="n">{counts['review']}</div><div class="l">Review</div></div>
  <div class="stat"><div class="n">{counts['reject']}</div><div class="l">Rejected</div></div>
  <div class="stat"><div class="n">{meta.get('fetched','—')}</div><div class="l">Listings found</div></div>
  <div class="stat"><div class="n">${cost:.4f}</div><div class="l">Cost</div></div>
</div>
<div class="tabs">
  <button class="tab" data-v="shortlist">Shortlist</button>
  <button class="tab" data-v="review">Review</button>
  <button class="tab" data-v="reject">Rejected</button>
  <button class="tab" data-v="all">All</button>
</div>
{''.join(rows) or '<p class="why">Nothing scored.</p>'}
<footer>
<strong>Review is a verdict, not a weak reject.</strong> It means a condition could not
be established — usually a missing start date. Those are worth a glance; they are the
listings a stricter filter would have thrown away.<br><br>
<strong>Fit and candidacy are separate on purpose.</strong> Fit is whether the role is
the right shape. Candidacy is whether you are competitive for it. One merged number
would tell you something failed without telling you which.
</footer>
</div>
<script>{JS}</script>"""

    path.write_text(doc, encoding="utf-8")
    if open_it:
        try:
            subprocess.run(["/usr/bin/open", str(path)], check=False)
        except Exception:
            webbrowser.open(path.as_uri())
    return path
