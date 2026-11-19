#!/usr/bin/env python3
"""
Universal Web Scraper Agent  (Chrome + local Ollama)
----------------------------------------------------
Works on any website. Instead of site-specific selectors, a small browsing
agent decides what to do on each page, step by step:

    type   -> type into the site's search box
    click  -> open a result / "reviews" link / etc.
    scroll -> reveal more content
    extract-> read the page text and pull out the data you asked for

Run:  python agent.py
It asks for: 1) URL  2) what to scrape  3) CSV file name, then asks to save.
"""
import argparse
import csv
import json
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

OLLAMA_URL = "http://localhost:11434/api/chat"
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "head"]
BLOCK_MARKERS = [
    "enter the characters you see below",
    "type the characters you see in this image",
    "verify you are human",
    "unusual traffic",
    "access denied",
]
KEYWORDS = ("review", "rating", "next", "search", "all ", "see more", "view all")


# --------------------------------------------------------------------------- #
# Browser helpers
# --------------------------------------------------------------------------- #
def auto_scroll(page, max_rounds=10, pause=0.6):
    """Scroll down until the page stops growing (lazy-loaded content)."""
    last = 0
    for _ in range(max_rounds):
        try:
            height = page.evaluate("document.body.scrollHeight")
            if height == last:
                break
            last = height
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            break
        time.sleep(pause)
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


def settle(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PWTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except PWTimeout:
        pass
    time.sleep(0.8)


def load_page(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PWTimeout:
        print("  ! navigation timeout, using whatever loaded", file=sys.stderr)
    settle(page)


def dismiss_popups(page):
    """Close login / cookie / promo pop-ups (e.g. Flipkart's login box)."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for sel in ['button:has-text("✕")', 'button[aria-label*="close" i]',
                'button[aria-label*="dismiss" i]']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click(timeout=1000)
        except Exception:
            pass


def find_next_url(page):
    js = """
    () => {
      const score = a => {
        const t = (a.innerText || '').trim().toLowerCase();
        const l = (a.getAttribute('aria-label') || '').toLowerCase();
        const r = (a.getAttribute('rel') || '').toLowerCase();
        if (r.includes('next')) return 10;
        if (l === 'next' || l.includes('next page')) return 9;
        if (['next','next page','next »','›','»','>'].includes(t)) return 8;
        if (t.startsWith('next')) return 6;
        return 0;
      };
      let best = null, bs = 0;
      for (const a of document.querySelectorAll('a[href]')) {
        const s = score(a); if (s > bs) { bs = s; best = a; }
      }
      return best ? best.href : null;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Seeing the page: clickable elements + cleaned text
# --------------------------------------------------------------------------- #
COLLECT_JS = """
() => {
  document.querySelectorAll('[data-agent-id]').forEach(e => e.removeAttribute('data-agent-id'));
  const vis = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const out = []; let n = 0;
  const sel = 'input, textarea, a[href], button, [role=button], [role=link]';
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && ['hidden','checkbox','radio','submit','image','file'].includes(el.type)) continue;
    const label = ((el.innerText || el.value || el.getAttribute('aria-label') ||
                    el.getAttribute('placeholder') || el.getAttribute('title') || '')
                   .trim().replace(/\\s+/g, ' ')).slice(0, 110);
    const ph = el.getAttribute('placeholder') || '';
    n += 1;
    el.setAttribute('data-agent-id', String(n));
    out.push({id: n, tag: tag, type: el.type || '', name: el.getAttribute('name') || '',
              placeholder: ph, label: label,
              href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 80) : ''});
  }
  return out;
}
"""


def collect_elements(page):
    try:
        return page.evaluate(COLLECT_JS)
    except Exception:
        return []


def pick_elements(elements, limit=55):
    """Keep the most informative elements so the small LLM isn't overwhelmed."""
    seen, scored = set(), []
    for e in elements:
        is_input = e["tag"] in ("input", "textarea")
        label = e["label"] or e["placeholder"]
        if not is_input and len(label) < 3:
            continue
        key = (e["tag"], label.lower())
        if key in seen:
            continue
        seen.add(key)
        low = label.lower()
        s = 5 if is_input else 0
        s += 3 if any(k in low for k in KEYWORDS) else 0
        s += min(len(label), 80) / 80
        scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    chosen = [e for _, e in scored[:limit]]
    chosen.sort(key=lambda e: e["id"])
    return chosen


def format_elements(chosen):
    lines = []
    for e in chosen:
        if e["tag"] in ("input", "textarea"):
            desc = f'[{e["id"]}] INPUT type={e["type"]} name={e["name"]} placeholder="{e["placeholder"]}"'
        elif e["tag"] == "a":
            desc = f'[{e["id"]}] LINK "{e["label"]}"'
        else:
            desc = f'[{e["id"]}] BUTTON "{e["label"]}"'
        lines.append(desc)
    return "\n".join(lines)


def structured_metadata(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            out.append(json.loads(tag.string or ""))
        except Exception:
            pass
    return out


def html_to_text(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(NOISE_TAGS):
        t.decompose()
    for t in soup.find_all(["nav", "footer", "aside"]):
        t.decompose()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        label = a.get_text(" ", strip=True)
        if label and not href.startswith("javascript"):
            a.replace_with(f" [{label}]({href}) ")
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt:
            img.replace_with(f" (image: {alt}) ")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{2,}", "\n", text)


def chunk_text(text, size=6000, overlap=300):
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + size])
        i += size - overlap
    return chunks


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
def ask_ollama(model, system, user, num_ctx=8192):
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=600)
    r.raise_for_status()
    return r.json()["message"]["content"]


def parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


PLANNER_SYSTEM = (
    "You control a web browser to complete a data-extraction task. "
    "Reply with ONE JSON object only: "
    '{"thought": "<short reason>", "action": "type|click|scroll|extract", '
    '"id": <element number or null>, "text": "<text to type or null>"}.\n'
    "Rules:\n"
    "- type: type into a SEARCH INPUT (use that input's id). text = ONLY the product/topic "
    "name from the task, e.g. 'asus vivobook 16'. Do this first when the current page is a "
    "homepage or does not show the product/topic the task is about.\n"
    "- click: open a link/button. On a search-results page click the result that best "
    "matches the product in the task. On a product page, if the task needs reviews or "
    "ratings, click the link to all reviews / ratings & reviews.\n"
    "- extract: ONLY when the visible page text already contains the data the task asks "
    "for (for reviews: real review texts with reviewer names).\n"
    "- scroll: when nothing useful is visible yet.\n"
    "Never repeat an action listed in HISTORY. Use only ids from ELEMENTS."
)


def plan_step(model, task, url, title, chosen, text, history):
    user = (
        f"TASK: {task}\n"
        f"CURRENT URL: {url}\n"
        f"PAGE TITLE: {title}\n"
        f"HISTORY:\n{chr(10).join(history[-6:]) or '(none yet)'}\n\n"
        f"ELEMENTS:\n{format_elements(chosen)}\n\n"
        f"PAGE TEXT (first 900 chars):\n{text[:900]}"
    )
    d = parse_json(ask_ollama(model, PLANNER_SYSTEM, user))
    d["action"] = str(d.get("action", "")).lower().strip()
    try:
        d["id"] = int(d["id"]) if d.get("id") is not None else None
    except (TypeError, ValueError):
        d["id"] = None
    return d


def derive_query(model, task):
    d = parse_json(ask_ollama(
        model,
        'Return JSON {"query": "..."}: the short product/topic search phrase contained in the task. '
        "Only the name itself, no words like review, rating, price, fetch.",
        task))
    return str(d.get("query") or task)


def extract_items(model, task, fields, chunk, url, title, meta_hint):
    field_line = (f"Use exactly these keys for every item: {fields}." if fields
                  else "Choose short, consistent snake_case keys, one per requested field.")
    system = (
        "You are a precise web data extraction engine. You read cleaned page text "
        'and return ONLY valid JSON: {"items": [ {...}, ... ]}. '
        "Never invent data; use null when a value is missing. "
        "Copy prices, ratings, names and review text exactly as they appear. "
        "Return only the fields the task asks for. "
        "If a value belongs to the whole page (e.g. the product price) repeat it on every item. "
        'If nothing relevant is in the text return {"items": []}.'
    )
    user = (
        f"Page URL: {url}\nPage title: {title}\n"
        f"TASK: {task}\n{field_line}\n"
        "Each distinct review/product/article/entry is one item. "
        "If the task asks for one single thing about the whole page, return one item.\n"
        f"{('Structured metadata: ' + meta_hint) if meta_hint else ''}\n\n"
        f"--- PAGE TEXT START ---\n{chunk}\n--- PAGE TEXT END ---"
    )
    data = parse_json(ask_ollama(model, system, user))
    items = data.get("items", []) if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict)]


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = json.dumps(it, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def find_search_input(elements):
    best = None
    for e in elements:
        if e["tag"] not in ("input", "textarea"):
            continue
        blob = f'{e["type"]} {e["name"]} {e["placeholder"]}'.lower()
        if "search" in blob or e["name"] in ("q", "query", "k", "s"):
            return e
        best = best or e
    return best


def do_type(page, element_id, text):
    loc = page.locator(f'[data-agent-id="{element_id}"]').first
    loc.click(timeout=6000)
    loc.fill(text, timeout=6000)
    page.keyboard.press("Enter")
    settle(page)


def do_click(page, element_id):
    """Click an element; stay in the same tab even if the site opens a new one."""
    loc = page.locator(f'[data-agent-id="{element_id}"]').first
    try:
        loc.evaluate("e => e.removeAttribute && e.removeAttribute('target')")
    except Exception:
        pass
    ctx = page.context
    before = len(ctx.pages)
    try:
        loc.click(timeout=6000)
    except Exception:
        loc.evaluate("e => e.click()")      # fallback when an overlay blocks the click
    time.sleep(1.5)
    if len(ctx.pages) > before:
        page = ctx.pages[-1]
    settle(page)
    return page


def extract_page(page, args):
    auto_scroll(page, 6, 0.5)
    html = page.content()
    low = html.lower()
    if any(m in low for m in BLOCK_MARKERS) and len(html) < 60000:
        print("  ! Page looks like a captcha / bot block. Solve it in the browser window.")
    meta = structured_metadata(html)
    meta_hint = json.dumps(meta, ensure_ascii=False)[:1500] if meta else ""
    title = page.title()
    text = html_to_text(html, page.url)
    chunks = chunk_text(text, size=args.chunk)
    print(f"  reading page: {len(text)} chars in {len(chunks)} chunk(s)")
    items = []
    for i, ch in enumerate(chunks, 1):
        print(f"  LLM extract {i}/{len(chunks)} ...", end="", flush=True)
        got = extract_items(args.model, args.task, args.fields, ch, page.url, title, meta_hint)
        print(f" {len(got)} item(s)")
        items.extend(got)
    return dedupe(items)


def scrape_more_pages(page, args, items):
    all_items = list(items)
    for n in range(2, args.pages + 1):
        nxt = find_next_url(page)
        if not nxt or nxt == page.url:
            print("  no next page found, stopping")
            break
        print(f"[page {n}] {nxt}")
        load_page(page, nxt)
        all_items.extend(extract_page(page, args))
    return dedupe(all_items)


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #
def agent_loop(page, args):
    history, signatures = [], set()
    for step in range(1, args.steps + 1):
        dismiss_popups(page)
        auto_scroll(page, 2, 0.4)
        elements = collect_elements(page)
        chosen = pick_elements(elements)
        html = page.content()
        text = html_to_text(html, page.url)
        title = page.title()
        print(f"\n[step {step}/{args.steps}] {page.url[:90]}")

        d = plan_step(args.model, args.task, page.url, title, chosen, text, history)
        action, eid, ptext = d["action"], d["id"], (d.get("text") or "").strip()
        by_id = {e["id"]: e for e in elements}

        # a search box is needed -> fall back to heuristics if the model picked badly
        if action == "type" and (eid not in by_id or by_id[eid]["tag"] not in ("input", "textarea")):
            si = find_search_input(elements)
            eid = si["id"] if si else None
            if eid is None:
                action = "scroll"
        if action == "type" and not ptext:
            ptext = derive_query(args.model, args.task)

        sig = f"{action}:{eid}:{ptext}"
        if sig in signatures and action != "extract":
            print("  (repeated action, scrolling instead)")
            action, eid = "scroll", None
        signatures.add(sig)
        thought = (d.get("thought") or "")[:100]
        label = by_id[eid]["label"][:60] if eid in by_id else ""

        try:
            if action == "type":
                print(f"  -> type '{ptext}' into [{eid}]  ({thought})")
                do_type(page, eid, ptext)
                history.append(f"typed '{ptext}' in search box and pressed Enter")
            elif action == "click" and eid in by_id:
                print(f"  -> click [{eid}] \"{label}\"  ({thought})")
                page = do_click(page, eid)
                history.append(f"clicked [{eid}] \"{label}\"")
            elif action == "extract":
                print(f"  -> extract  ({thought})")
                items = extract_page(page, args)
                if items:
                    return page, scrape_more_pages(page, args, items)
                history.append("tried extract here but found nothing; navigate further")
                print("  nothing found here, continuing")
            else:
                print("  -> scroll")
                page.mouse.wheel(0, 1500)
                time.sleep(0.8)
                history.append("scrolled down")
        except Exception as ex:
            print(f"  ! action failed: {str(ex)[:120]}")
            history.append(f"{action} failed ({str(ex)[:60]})")

    print("\nStep limit reached, trying a last extraction on the current page.")
    items = extract_page(page, args)
    return page, scrape_more_pages(page, args, items) if items else []


def run(args):
    with sync_playwright() as p:
        if args.cdp:
            browser = p.chromium.connect_over_cdp(args.cdp)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=args.profile, channel="chrome",
                headless=args.headless, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        print(f"[open] {args.url}")
        load_page(page, args.url)
        _, items = agent_loop(page, args)
        if not args.cdp:
            ctx.close()
    return items


# --------------------------------------------------------------------------- #
# Saving + interactive prompts
# --------------------------------------------------------------------------- #
def save_csv(items, filename):
    keys = []
    for it in items:
        for k in it:
            if k not in keys:
                keys.append(k)
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for it in items:
            w.writerow({k: json.dumps(v, ensure_ascii=False)
                        if isinstance(v, (dict, list)) else v for k, v in it.items()})


def ask(prompt, default=None):
    while True:
        suffix = f" [{default}]" if default else ""
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default:
            return default
        print("  This can't be empty.")


def clean_url(u):
    u = u.strip().strip('"').strip("'")
    return u if re.match(r"^https?://", u, re.I) else "https://" + u


def clean_filename(name):
    name = name.strip().strip('"').strip("'")
    return name if name.lower().endswith(".csv") else name + ".csv"


def main():
    ap = argparse.ArgumentParser(description="Universal AI web scraper (Ollama + Chrome)")
    ap.add_argument("--fields", help="Optional fixed keys, e.g. reviewer,rating,review,price")
    ap.add_argument("--pages", type=int, default=1, help="Result pages to read via 'Next'")
    ap.add_argument("--steps", type=int, default=8, help="Max browsing steps before extracting")
    ap.add_argument("--model", default="qwen3.5:4b")
    ap.add_argument("--cdp", help="Attach to your running Chrome, e.g. http://localhost:9222")
    ap.add_argument("--profile", default="./chrome_profile")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--chunk", type=int, default=6000)
    args = ap.parse_args()

    print("=== Universal Web Scraper Agent ===\n")
    args.url = clean_url(ask("1) URL of the website"))
    args.task = ask("2) What do you want to scrape (e.g. product name, price, rating)")
    filename = clean_filename(ask("3) File name to store the scraped data", "scraped_data"))

    print("\nWorking... a Chrome window will open and the agent will browse for you.\n")
    items = run(args)

    if not items:
        print("\nNothing was extracted. Try a more specific description, "
              "raise --steps, or check whether the site showed a captcha.")
        return

    print(f"\nScraped {len(items)} item(s). Preview of the first 3:")
    for it in items[:3]:
        print("  ", json.dumps(it, ensure_ascii=False))

    answer = input(f"\nSave the data to '{filename}' as CSV? (y/n) [y]: ").strip().lower()
    if answer in ("", "y", "yes"):
        save_csv(items, filename)
        print(f"Saved {len(items)} row(s) -> {filename}")
    else:
        print("Okay, not saved.")


if __name__ == "__main__":
    main()