"""Scrapers for Eluta and Hiring Cafe. UNTESTED against the live sites (blocked in the build sandbox).

Run `python jobhunter.py probe eluta` first: it saves page HTML + a screenshot to ./probe/
so selectors/URL params below can be corrected. Needs: pip install playwright && playwright install chromium
"""
import json, re, time
from pathlib import Path
from urllib.parse import quote_plus

# Three keyword sets: key-account titles (CPG/foodservice) vs. SaaS-style titles.
QUERIES = ["key account manager", "account manager", "client success manager", "customer success manager"]
INDUSTRY_TERMS = ["", "CPG", "foodservice", "SaaS", "fintech"]   # "" = no industry term
LOCATIONS = ["Ajax, ON", "Pickering, ON", "Whitby, ON", "Scarborough, ON", "Toronto, ON",
             "Markham, ON", "North York, ON", "Oshawa, ON", "Canada"]  # "Canada" catches remote

# VERIFY these URL patterns in a browser; adjust params if the site ignores them.
SITES = {
    "eluta": {
        "url": "https://www.eluta.ca/search?q={q}&l={loc}&posted=7",   # 'posted=7' = last 7 days (verify)
        "scrolls": 0, "pages": 3,
    },
    "hiringcafe": {
        # Hiring Cafe encodes search state in the URL. Easiest: set filters in the UI
        # (title, location, date posted = last 7 days), copy the URL, paste it here.
        "url": "https://hiring.cafe/?searchState=PASTE_YOUR_FILTERED_URL_STATE_HERE",
        "scrolls": 8, "pages": 1,
    },
}

# Generic card finder: any repeated block containing a link and a title-ish first line.
EXTRACT_JS = """
() => {
  const out = [], seen = new Set();
  // 1) schema.org JobPosting JSON-LD, if present
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { const d = JSON.parse(s.textContent); (Array.isArray(d)?d:[d]).forEach(x => {
      if (x && x['@type']==='JobPosting') out.push({title:x.title, company:(x.hiringOrganization||{}).name||'',
        location:JSON.stringify(x.jobLocation||''), posted:x.datePosted||'', url:x.url||location.href,
        description:(x.description||'').replace(/<[^>]+>/g,' ').slice(0,3000)});
    });} catch(e){}
  });
  // 2) heuristic cards
  const sel = 'article, li, div[class*="job" i], div[class*="result" i], div[class*="card" i]';
  document.querySelectorAll(sel).forEach(el => {
    const a = el.querySelector('a[href]'); if (!a) return;
    const text = el.innerText.trim(); if (text.length < 30 || text.length > 900) return;
    if (el.querySelectorAll('a[href]').length > 6) return;       // skip containers/lists
    const href = a.href; if (seen.has(href)) return; seen.add(href);
    const lines = text.split('\\n').map(s=>s.trim()).filter(Boolean);
    const posted = (text.match(/(\\d+\\+?\\s*(?:day|week|month|hour)s?\\s*ago|today|yesterday|just posted)/i)||[''])[0];
    out.push({title: (a.innerText.trim() || lines[0]).slice(0,140), company: lines[1]||'',
              location: lines.find(l=>/,\\s*(ON|Ontario)|remote|hybrid/i.test(l))||'',
              posted, url: href, description: text});
  });
  return out;
}
"""

def _pw():
    from playwright.sync_api import sync_playwright
    return sync_playwright()

def scrape_all():
    jobs = []
    with _pw() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                            locale="en-CA")
        page = ctx.new_page()
        # Eluta: one search per query x location (industry words go in the query).
        cfg = SITES["eluta"]
        for q in QUERIES:
            for loc in LOCATIONS:
                url = cfg["url"].format(q=quote_plus(q), loc=quote_plus(loc))
                jobs += _grab(page, url, "eluta", cfg["scrolls"])
                time.sleep(2)                                  # be polite
        # Hiring Cafe: one pre-filtered URL, scroll to load more.
        cfg = SITES["hiringcafe"]
        if "PASTE_YOUR" not in cfg["url"]:
            jobs += _grab(page, cfg["url"], "hiringcafe", cfg["scrolls"])
        else:
            print("Hiring Cafe URL not set in sources.py - skipping")
        b.close()
    print(f"scraped {len(jobs)} raw cards")
    return jobs

def _grab(page, url, source, scrolls):
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        for _ in range(scrolls):
            page.mouse.wheel(0, 4000); time.sleep(1.2)
        rows = page.evaluate(EXTRACT_JS)
    except Exception as e:
        print(f"[{source}] failed {url}: {type(e).__name__}")
        return []
    for r in rows:
        r["source"] = source
    return rows

def probe(source):
    out = Path(__file__).parent / "probe"; out.mkdir(exist_ok=True)
    cfg = SITES[source]
    url = cfg["url"].format(q="account+manager", loc="Toronto%2C+ON") if source == "eluta" else cfg["url"]
    with _pw() as p:
        b = p.chromium.launch(headless=True); page = b.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        (out / f"{source}.html").write_text(page.content())
        page.screenshot(path=str(out / f"{source}.png"), full_page=True)
        rows = page.evaluate(EXTRACT_JS)
        (out / f"{source}.parsed.json").write_text(json.dumps(rows[:20], indent=1))
        print(f"{source}: {len(rows)} cards parsed. Saved HTML/screenshot/sample to {out}/")
        b.close()
