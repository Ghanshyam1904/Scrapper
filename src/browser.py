import asyncio
import inspect
import json
import os
import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from playwright.async_api import async_playwright

from src.config import (
    USER_DATA_DIR,
    PROXY_URL,
    PAGE_CACHE,
    DOM_SAMPLE_CACHE,
    ERROR_PHRASES,
    BOT_BLOCK_PHRASES,
    MANDATORY_LOGIN_PHRASES,
    QUORA_GATE_MARKERS,
    LINKEDIN_GATE_MARKERS,
    DISMISS_POPUPS_JS,
)

_TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "_ga", "ref_", "pf_rd_", "pd_rd_")
_TRACKING_PARAM_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "srsltid", "trk", "trackingid",
    "qid", "sprefix", "crid", "dib", "dib_tag", "otracker", "otracker1",
    "ppt", "ppn", "ssid", "_hsenc", "_hsmi", "igshid",
}

SMART_SCROLL_JS = """
() => {
    const docEl = document.documentElement;
    const beforeWinY = window.scrollY;
    window.scrollBy(0, window.innerHeight * 0.9);
    if (window.scrollY > beforeWinY + 10) {
        window.dispatchEvent(new Event('scroll'));
        return 'window';
    }
    let best = null;
    let bestScore = 0;
    for (const el of document.querySelectorAll('div, main, section, ul')) {
        try {
            const st = getComputedStyle(el);
            if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 80) {
                const score = el.clientHeight * (el.scrollHeight - el.clientHeight);
                if (score > bestScore) {
                    bestScore = score;
                    best = el;
                }
            }
        } catch (e) {}
    }
    if (best) {
        best.scrollTop = best.scrollTop + Math.max(400, best.clientHeight * 0.85);
        best.dispatchEvent(new Event('scroll'));
        return 'container';
    }
    window.scrollTo(0, Math.max(document.body.scrollHeight, docEl.scrollHeight));
    window.dispatchEvent(new Event('scroll'));
    return 'window_bottom';
}
"""


def _strip_tracking_params(url_str: str) -> str:
    try:
        parsed = urlparse(url_str)
        if not parsed.query:
            return url_str
        kept = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
            and k.lower() not in _TRACKING_PARAM_EXACT
        ]
        return urlunparse(parsed._replace(query=urlencode(kept, doseq=True)))
    except Exception:
        return url_str


def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"data:image/[^\s)]+", "", text)
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)

    text = re.sub(
        r"(https?://(?:www\.)?amazon\.[a-z.]+/)(?:[^)\s\"']*/)?dp/([A-Z0-9]{10})[^)\s\"']*",
        r"\1dp/\2",
        text,
    )
    text = re.sub(
        r"(https?://(?:www\.)?flipkart\.com/[^)\s\"']+/p/itm[a-zA-Z0-9]+)\?[^)\s\"']*?\b(pid=[A-Za-z0-9]+)[^)\s\"']*",
        r"\1?\2",
        text,
    )
    text = re.sub(
        r"(https?://(?:[a-z]+\.)?linkedin\.com/jobs/view/[^?\s)\"']+)\?[^)\s\"']+",
        r"\1",
        text,
    )

    text = re.sub(r"https?://[^\s)\"'>]+", lambda m: _strip_tracking_params(m.group(0)), text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_structured_data(raw_html: str) -> str:
    """Extract Schema.org JSON-LD objects and clean HTML tables when present."""
    if not raw_html or len(raw_html) < 200:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    parts: list[str] = []
    try:
        soup = BeautifulSoup(raw_html, "html.parser")

        # 1. JSON-LD extraction
        ld_summaries: list[str] = []
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            raw_json = (script.string or script.get_text() or "").strip()
            if not raw_json:
                continue
            try:
                data = json.loads(raw_json)
            except Exception:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if "@graph" in node and isinstance(node["@graph"], list):
                    nodes.extend(n for n in node["@graph"] if isinstance(n, dict))
                    continue
                t = str(node.get("@type", ""))
                if not t or t in ("WebSite", "WebPage", "BreadcrumbList", "Organization", "SearchAction"):
                    continue
                compact = {
                    k: v
                    for k, v in node.items()
                    if k not in ("@context", "image", "potentialAction", "mainEntityOfPage")
                    and v not in (None, "", [], {})
                }
                if compact:
                    s = json.dumps(compact, ensure_ascii=False)
                    if len(s) > 700:
                        s = s[:700] + "..."
                    ld_summaries.append(s)
                if len(ld_summaries) >= 3:
                    break

        if ld_summaries:
            parts.append("[JSON-LD STRUCTURED DATA]\n" + "\n".join(ld_summaries))

        # 2. Clean HTML Table extraction (Wikipedia, Worldometers, Sports stats)
        table_blocks: list[str] = []
        for tbl in soup.find_all("table"):
            rows = tbl.find_all("tr")
            if len(rows) < 3:
                continue
            md_rows: list[str] = []
            for r_idx, tr in enumerate(rows[:35]):
                cells = [
                    re.sub(r"\s+", " ", c.get_text(" ", strip=True)).replace("|", "/")[:60]
                    for c in tr.find_all(["th", "td"])
                ]
                if len(cells) < 2:
                    continue
                md_rows.append("| " + " | ".join(cells) + " |")
                if r_idx == 0:
                    md_rows.append("| " + " | ".join("---" for _ in cells) + " |")
            if len(md_rows) >= 3:
                table_blocks.append("\n".join(md_rows))
            if len(table_blocks) >= 2:
                break

        if table_blocks:
            parts.append("[HTML TABLES EXTRACTED]\n" + "\n\n".join(table_blocks))

    except Exception:
        return ""

    out = "\n\n".join(parts)
    return out[:3500]


def extract_dom_sample(raw_html: str) -> str:
    """Return a compact 1-item HTML skeleton of real repeating cards/rows (including classless tables & data-testid)."""
    if not raw_html or len(raw_html) < 200:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for bad in soup.select("script, style, noscript, svg, nav, footer"):
            bad.decompose()

        sig_counts: Counter = Counter()
        first_node: dict = {}
        for tag in soup.find_all(["div", "article", "li", "tr", "shreddit-post"]):
            classes = tuple(tag.get("class") or [])
            testid = str(tag.get("data-testid") or tag.get("data-component-type") or "")
            is_table_row = tag.name == "tr" and len(tag.find_all("td")) >= 2
            if not classes and not testid and not is_table_row and tag.name not in ("article", "shreddit-post"):
                continue

            txt_len = len(tag.get_text(" ", strip=True))
            if txt_len < 20 or txt_len > 2000:
                continue
            if len(tag.find_all(True)) < 2:
                continue

            sig = (tag.name, classes, testid)
            sig_counts[sig] += 1
            if sig not in first_node:
                first_node[sig] = tag

        scored = []
        for sig, cnt in sig_counts.items():
            if cnt < 3:
                continue
            node = first_node[sig]
            has_link = 1 if node.find("a", href=True) else 0
            has_testid = 1 if sig[2] else 0
            score = (has_link * 45) + (has_testid * 35) + min(cnt, 25) * min(len(node.find_all(True)), 8)
            scored.append((score, sig))

        scored.sort(key=lambda x: x[0], reverse=True)
        for _, sig in scored[:5]:
            sample = first_node[sig]
            clone = BeautifulSoup(str(sample), "html.parser")
            for el in clone.find_all(True):
                kept = {}
                for attr in ("id", "class", "data-testid", "data-component-type", "itemprop", "role",
                             "data-rating", "data-count", "data-score", "data-value", "aria-label"):
                    if el.get(attr):
                        kept[attr] = el.get(attr)
                if el.get("title"):
                    kept["title"] = str(el.get("title"))[:35]
                if el.name == "a" and el.get("href"):
                    kept["href"] = str(el.get("href"))[:45]
                el.attrs = kept
                if el.string and len(el.string.strip()) > 28:
                    el.string.replace_with(el.string.strip()[:28] + "...")
            snippet = re.sub(r"\s+", " ", str(clone)).strip()
            if 40 <= len(snippet) <= 900:
                return snippet
        return ""
    except Exception:
        return ""


def filter_kwargs(cls, kwargs: dict) -> dict:
    try:
        sig = inspect.signature(cls.__init__)
        return {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        return kwargs


def resolve_spa_url(url: str, main_url: str = "") -> str:
    if "flipkart.com/ratings-reviews-details-page" in url.lower():
        pid_match = re.search(r"pid=([A-Za-z0-9]+)", url)
        target_pid = pid_match.group(1) if pid_match else ""
        candidates = [main_url] + list(PAGE_CACHE.keys())

        for pass_exact in (True, False):
            for cand in candidates:
                if not cand:
                    continue
                m = re.match(r"^(https?://(?:www\.)?flipkart\.com/[^/]+)/p/(itm[a-zA-Z0-9]+)", cand)
                if not m:
                    continue
                if pass_exact and target_pid and target_pid not in cand:
                    continue
                pid_q = f"?pid={target_pid}" if target_pid else ""
                resolved = f"{m.group(1)}/product-reviews/{m.group(2)}{pid_q}"
                print(f"  [URL Fixed] Broken Flipkart route -> '{resolved}'")
                return resolved
    return url


async def _wait_for_profile_unlock(user_data_dir: str, max_wait: float = 4.0) -> None:
    lock_candidates = ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket")
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        busy = False
        for fname in lock_candidates:
            fpath = os.path.join(user_data_dir, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except PermissionError:
                    busy = True
                except OSError:
                    pass
        if not busy:
            return
        await asyncio.sleep(0.4)


async def launch_persistent_browser(p, headless: bool):
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    await _wait_for_profile_unlock(USER_DATA_DIR)

    common: dict = {
        "user_data_dir": USER_DATA_DIR,
        "headless": headless,
        "timeout": 15000,
        "viewport": {"width": 1366, "height": 850},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    }
    if PROXY_URL:
        common["proxy"] = {"server": PROXY_URL}

    errors: list[str] = []
    # Only Chrome should share Chrome's USER_DATA_DIR (Edge fails with Settings version error)
    for ch in ("chrome", None):
        try:
            if ch:
                return await p.chromium.launch_persistent_context(channel=ch, **common)
            return await p.chromium.launch_persistent_context(**common)
        except Exception as e:
            errors.append(f"{ch or 'chromium'}: {e}")
            print(f"  [Browser Launch Warning] channel={ch or 'chromium'} failed: {e}")

    print("  [Browser Fallback] Persistent profile locked; launching clean temporary browser...")
    launch_opts: dict = {
        "headless": headless,
        "timeout": 15000,
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if PROXY_URL:
        launch_opts["proxy"] = {"server": PROXY_URL}

    clean_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    for ch in ("chrome", "msedge", None):
        try:
            browser = await (p.chromium.launch(channel=ch, **launch_opts) if ch else p.chromium.launch(**launch_opts))
            return await browser.new_context(viewport={"width": 1366, "height": 850}, user_agent=clean_ua)
        except Exception as e:
            errors.append(f"temp-{ch or 'chromium'}: {e}")

    raise RuntimeError("Could not launch Chromium/Chrome/Edge via Playwright.\n" + "\n".join(errors))

class BrowserManager:
    """Single-context manager per run to reuse browser sessions and prevent USER_DATA_DIR lock collisions."""

    def __init__(self):
        self._playwright = None
        self._context = None
        self._headless: bool | None = None
        self._auth_cookies: list[dict] = []

    def is_active(self) -> bool:
        return self._context is not None

    def set_auth_cookies(self, cookies: list[dict]) -> None:
        self._auth_cookies = list(cookies or [])

    def has_auth_cookies(self) -> bool:
        return bool(self._auth_cookies)

    def clear_auth_cookies(self) -> None:
        self._auth_cookies = []

    async def get_page(self, headless: bool = True):
        if os.getenv("FORCE_HEADED", "0") == "1":
            headless = False
        if self._context is not None and self._headless != headless:
            await self.close()

        if self._context is None:
            self._playwright = await async_playwright().start()
            try:
                self._context = await launch_persistent_browser(self._playwright, headless=headless)
                self._headless = headless
                await self._context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = window.chrome || { runtime: {} };
                """)
                if self._auth_cookies:
                    try:
                        await self._context.add_cookies(self._auth_cookies)
                    except Exception:
                        pass
            except Exception:
                await self.close()
                raise

        live_pages = [p for p in self._context.pages if not p.is_closed()]
        page = live_pages[0] if live_pages else await self._context.new_page()
        for extra in live_pages[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        return self._context, page

    async def close(self, clear_auth: bool = False) -> None:
        if clear_auth:
            self._auth_cookies = []
        if self._context is not None:
            parent_browser = getattr(self._context, "browser", None)
            try:
                await self._context.close()
            except Exception:
                pass
            if parent_browser is not None:
                try:
                    await parent_browser.close()
                except Exception:
                    pass
            self._context = None
            self._headless = None
            if os.name == "nt":
                await asyncio.sleep(1.0)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


BROWSER_MANAGER = BrowserManager()


async def extract_dom_markdown(page, skip_scroll: bool = False) -> str:
    try:
        await page.evaluate(DISMISS_POPUPS_JS)
        await page.wait_for_timeout(400)
    except Exception:
        pass

    if not skip_scroll:
        try:
            for _ in range(3):
                await page.evaluate(SMART_SCROLL_JS)
                await page.wait_for_timeout(400)
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

    raw = ""
    for attempt in range(2):
        try:
            raw = await page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, aside, footer, script, style, noscript, svg, iframe, template, [hidden], [aria-hidden="true"], dialog:not([open])').forEach(el => {
                    const hasPag = el.querySelector('.pagination, .page-link, .next, [aria-label*="next" i]')
                        || (el.getAttribute('aria-label') || '').toLowerCase().includes('pag');
                    if (!hasPag) el.remove();
                });

                clone.querySelectorAll('a').forEach(a => {
                    let txt = (a.innerText || '').trim().replace(/\\s+/g, ' ');
                    const fullTitle = (a.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
                    if (fullTitle.length > txt.length && (txt.endsWith('...') || txt.endsWith('…'))) {
                        txt = fullTitle;
                    }
                    const href = a.href || '';
                    if (txt && href && href.startsWith('http') && !href.includes('javascript:')) {
                        a.replaceWith(document.createTextNode(`${txt} (${href}) `));
                    } else if (txt) {
                        a.replaceWith(document.createTextNode(`${txt} `));
                    }
                });

                clone.querySelectorAll('div.thumbnail, article, .product-item, .card, .quote, tr.athing, shreddit-post').forEach((card, idx) => {
                    card.insertAdjacentText('beforebegin', `\\n--- ITEM ${idx + 1} ---\\n`);
                    card.insertAdjacentText('afterend', '\\n');
                });

                return clone.innerText || '';
            }
            """)
            break
        except Exception:
            if attempt == 0:
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
                await page.wait_for_timeout(1200)
            else:
                raise

    final_url = page.url or ""
    cleaned = clean_markdown(raw or "")
    if is_login_url(final_url):
        cleaned = f"[REDIRECTED TO LOGIN PAGE: {final_url}]\n" + cleaned
    return cleaned


async def handle_human_verification(page) -> bool:
    """Silently auto-click simple 'Continue shopping' interstitials without opening tabs or pausing."""
    try:
        body_text = await page.inner_text("body")
    except Exception:
        body_text = ""

    low = (body_text or "").lower()
    if "click the button below to continue shopping" in low:
        for sel in (
            "button:has-text('Continue shopping')",
            "a:has-text('Continue shopping')",
            "input[type='submit'][value*='Continue' i]",
        ):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=800):
                    await loc.click()
                    await page.wait_for_timeout(3000)
                    print("  [Interstitial] Auto-clicked 'Continue shopping'.")
                    return True
            except Exception:
                pass
    return False


async def playwright_stealth_fetch_full(url: str) -> tuple[str, str]:
    print(f"  [Fallback] Crawl4AI blocked/empty, retrying with headless stealth browser for '{url[:60]}...'")
    _, page = await BROWSER_MANAGER.get_page(headless=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    await page.wait_for_timeout(3500)

    for _ in range(3):
        body_low = (await page.inner_text("body")).lower()
        if any(b in body_low for b in ("just a moment", "checking your browser", "checking if the site connection is secure")):
            await page.wait_for_timeout(3000)
        else:
            break

    await handle_human_verification(page)
    md = await extract_dom_markdown(page)
    raw_html = await page.content()
    return md, raw_html


def is_404_page(text: str) -> bool:
    low = text.lower()
    total_len = len(text)
    if total_len < 12000 and any(e in low for e in ERROR_PHRASES):
        return True
    if any(e in low[:3500] for e in ERROR_PHRASES) and total_len < 18000:
        return True
    return False


def is_bot_blocked(text: str) -> bool:
    if is_404_page(text) or is_mandatory_login_wall(text):
        return False
    low = text.lower()
    if len(text) < 350:
        if any(w in low for w in ("bypassed", "congratulations", "success")):
            return False
        return True
    if len(text) < 12000 and any(p in low[:6000] for p in BOT_BLOCK_PHRASES):
        return True
    return False


def is_mandatory_login_wall(text: str) -> bool:
    low = text.lower()
    total_len = len(text)
    head = low[:6000]

    if "[redirected to login page" in low[:500]:
        return True

    if any(p in head for p in BOT_BLOCK_PHRASES):
        return False

    quora_hits = sum(1 for m in QUORA_GATE_MARKERS if m in low[:10000])
    quora_content_signals = sum(1 for s in ("upvote", "answered ", "credentials", "related questions", "follow") if s in low)
    if quora_hits >= 2 and quora_content_signals <= 1:
        return True

    linkedin_hits = sum(1 for m in LINKEDIN_GATE_MARKERS if m in low[:8000])
    linkedin_job_signals = sum(1 for s in ("/jobs/view/", "applicants", "easy apply", "ago") if s in low)
    if linkedin_hits >= 2 and linkedin_job_signals == 0:
        return True

    if any(p in head for p in MANDATORY_LOGIN_PHRASES):
        return True
    if total_len < 6000 and any(p in low for p in MANDATORY_LOGIN_PHRASES):
        return True

    if total_len < 600 and any(
            k in low for k in ("log in", "login", "sign in", "sign up", "create account", "password")):
        return True

    return False


def is_login_url(url: str) -> bool:
    low = (url or "").lower()
    return any(x in low for x in ("ap/signin", "/login", "/signin", "account/login", "/uas/login", "/authwall", "/checkpoint/"))


async def verify_authenticated_session(page, context, original_url: str) -> tuple[bool, str, str, str]:
    active_url = (page.url or original_url).split("#")[0]
    orig_clean = original_url.split("#")[0]

    if not is_login_url(orig_clean) and not is_login_url(active_url) and orig_clean.rstrip("/") != active_url.rstrip("/"):
        if not any(r in active_url.lower() for r in ("/inventory", "/dashboard", "/home", "/feed")):
            try:
                await page.goto(orig_clean, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2500)
                active_url = (page.url or orig_clean).split("#")[0]
            except Exception as e:
                print(f"  [Verify Auth Warning] Could not reload '{orig_clean}': {e}")

    try:
        has_visible_password = await page.locator("input[type='password']:visible").count() > 0
    except Exception:
        has_visible_password = False

    logout_found = False
    for sel in (
        "a:has-text('Logout')",
        "a:has-text('Log out')",
        "a:has-text('Sign out')",
        "button:has-text('Logout')",
        "button:has-text('Log out')",
        "button:has-text('Sign out')",
    ):
        try:
            if await page.locator(sel).count() > 0:
                logout_found = True
                break
        except Exception:
            pass

    domain = urlparse(active_url).netloc.lower().lstrip("www.")
    try:
        all_cookies = await context.cookies()
        domain_cookies = [c for c in all_cookies if domain and domain in c.get("domain", "").lower()]
    except Exception:
        domain_cookies = []

    unlocked_text = await extract_dom_markdown(page)
    try:
        DOM_SAMPLE_CACHE[active_url] = extract_dom_sample(await page.content())
    except Exception:
        pass

    still_login_url = is_login_url(active_url)
    wall_present = is_mandatory_login_wall(unlocked_text)
    has_content = len(unlocked_text) >= 350

    url_changed = active_url.rstrip("/") != orig_clean.rstrip("/")
    has_proof = logout_found or url_changed or (len(domain_cookies) >= 1 and has_content)
    auth_verified = (not has_visible_password) and (not still_login_url) and (not wall_present) and has_content and has_proof

    summary = (
        f"url='{active_url}', visible_password_form={has_visible_password}, still_on_login_url={still_login_url}, "
        f"logout_indicator={logout_found}, domain_cookies={len(domain_cookies)}, "
        f"login_wall_present={wall_present}, content_chars={len(unlocked_text)}"
    )
    return auth_verified, active_url, unlocked_text, summary