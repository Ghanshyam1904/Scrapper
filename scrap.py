import asyncio
import csv
import inspect
import json
import os
import re
import time
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

from groq import Groq, APIStatusError
from ollama import Client as OllamaClient
from playwright.async_api import async_playwright
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

# ---------------- CONFIG FROM .ENV ----------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b").strip()
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "system_prompt.txt").strip()
DEFAULT_LOGIN_EMAIL = os.getenv("LOGIN_EMAIL", "").strip()

USER_DATA_DIR = os.path.abspath("./browser_profile")
MEMORY_FILE = os.path.abspath("./agent_memory.json")
DATA_DIR = os.path.abspath("./data")

GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]
CURRENT_GROQ_IDX = 0

ollama_client = OllamaClient() if LLM_PROVIDER == "ollama" else None
groq_client = None
if LLM_PROVIDER == "groq":
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key or groq_key.startswith(("gsk_your", "gsk_placeholder", "gsk_aapki")):
        raise ValueError("GROQ_API_KEY is not properly set in the .env file!")
    groq_client = Groq(api_key=groq_key)
elif LLM_PROVIDER != "ollama":
    raise ValueError(f"Unsupported LLM_PROVIDER='{LLM_PROVIDER}'. Use 'ollama' or 'groq'.")

PAGE_CACHE: dict[str, str] = {}

ERROR_PHRASES = (
    "moved or deleted",
    "page not found",
    "404 not found",
    "this page doesn't exist",
    "the page you are looking for",
    "unfortunately the page you are looking for",
)

BOT_BLOCK_PHRASES = (
    "just a moment...",
    "checking your browser",
    "verify you are human",
    "are you a human",
    "access denied",
    "403 forbidden",
    "enable javascript and cookies",
    "unusual traffic from your computer",
    "attention required! | cloudflare",
    "pardon our interruption",
)

MANDATORY_LOGIN_PHRASES = (
    "[redirected to login page",
    "log in to continue",
    "sign in to continue",
    "sign in to view",
    "create an account to continue",
    "login to view",
    "log in or sign up",
    "please log in to",
    "you need to log in",
    "you must log in",
    "join to see",
    "sign up to read",
    "sign in to access",
)

QUORA_GATE_MARKERS = (
    "by continuing you indicate that you agree to quora",
    "sign up with email",
    "continue with google",
    "a place to share knowledge and better understand the world",
    "forgot password?",
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a precision autonomous web scraping AI agent. "
    "Always call scrape_website first on the Target URL. "
    "If the user asks for a topic or item not present on a homepage, call search_on_website. "
    "Use save_memory_note to store partial facts (like price/rating) while searching for remaining fields (like reviews). "
    "Check [AGENT WORKING MEMORY] at the top before every step to see pending URLs and already-saved items. "
    "ONLY if scrape_website or search_on_website reports [MANDATORY LOGIN WALL DETECTED] or redirects to sign-in, "
    "call login_or_signup_with_email once then re-scrape/re-search. "
    "If public data is visible, NEVER ask for login. "
    "Never call login_or_signup_with_email on a 404 or 'moved or deleted' page. "
    "Extract ONLY requested fields, save via save_to_csv immediately after EACH item, and never invent fake data."
)

DISMISS_POPUPS_JS = """
(() => {
    try {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27, bubbles: true }));
        const closeSelectors = [
            "button[aria-label*='close' i]",
            "span[role='button']",
            "button._2KpZ6l._2doB4z",
            "span._30XB9F",
            "[class*='close' i][role='button']",
            "button:has-text('No thanks')",
            "button:has-text('Skip')"
        ];
        for (const sel of closeSelectors) {
            try {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null && (el.innerText.trim().length <= 12 || sel.includes('close') || sel.includes('30XB9F'))) {
                    el.click();
                }
            } catch (e) {}
        }
    } catch (e) {}
})();
"""


def resolve_csv_path(filename: str) -> str:
    name = (filename or "output.csv").strip() or "output.csv"
    if not name.lower().endswith(".csv"):
        name += ".csv"
    if os.path.isabs(name):
        return name
    norm = os.path.normpath(name)
    if norm.startswith(f"data{os.sep}") or norm == "data":
        return os.path.abspath(norm)
    return os.path.join(DATA_DIR, norm)


# ---------------- WORKING & PERSISTENT MEMORY ----------------
def _load_site_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _save_site_memory(data: dict) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().lstrip("www.")


class WorkingMemory:
    def __init__(self, target_url: str):
        self.target_url = target_url.split("#")[0]
        self.domain = _domain_of(target_url)
        self.visited_urls: list[str] = []
        self.broken_urls: list[str] = []
        self.pending_urls: list[str] = []
        self.notes: list[str] = []
        self.saved_summaries: list[str] = []
        self.site_memory: dict = _load_site_memory()

    def mark_visited(self, url: str) -> None:
        u = url.split("#")[0]
        if u not in self.visited_urls:
            self.visited_urls.append(u)
        if u in self.pending_urls:
            self.pending_urls.remove(u)

    def mark_broken(self, url: str) -> None:
        u = url.split("#")[0]
        if u not in self.broken_urls:
            self.broken_urls.append(u)
        if u in self.pending_urls:
            self.pending_urls.remove(u)

    def extract_candidate_urls(self, text: str, source_url: str) -> None:
        raw_urls = re.findall(r"https?://[^\s)\"'>]+", text)
        ignore_parts = (
            "/login", "/signin", "/signup", "/ap/signin", "/ap/register",
            "/help", "/privacy", "/terms", "/cart", "/wishlist", "/account",
            "ratings-reviews-details-page", ".png", ".jpg", ".jpeg", ".webp",
            ".svg", ".css", ".js", "amazon.com/gp/goldbox",
        )
        src_clean = source_url.split("#")[0].rstrip("/")
        for u in raw_urls:
            u_clean = u.split("#")[0].rstrip(".,;:")
            low = u_clean.lower()
            if _domain_of(u_clean) != self.domain:
                continue
            if u_clean.rstrip("/") == src_clean or u_clean in self.visited_urls or u_clean in self.broken_urls:
                continue
            if any(bad in low for bad in ignore_parts):
                continue
            path = urlparse(u_clean).path or "/"
            is_interesting = (
                "/dp/" in low
                or "/p/itm" in low
                or "/product-reviews/" in low
                or "/answer/" in low
                or "/article" in low
                or "/blog" in low
                or (path.count("/") >= 1 and len(path) >= 20)
            )
            if is_interesting and u_clean not in self.pending_urls:
                self.pending_urls.append(u_clean)
                if len(self.pending_urls) >= 12:
                    break

    def add_note(self, note: str) -> str:
        note = note.strip()
        if not note:
            return "Empty note ignored."
        if note not in self.notes:
            self.notes.append(note)
            if len(self.notes) > 10:
                self.notes = self.notes[-10:]
        return f"Memory note saved ({len(self.notes)} total notes)."

    def record_saved_rows(self, items: list[dict]) -> None:
        for item in items:
            compact = ", ".join(f"{k}={str(v)[:50]}" for k, v in item.items())
            self.saved_summaries.append(compact)

    def remember_keyword(self, keyword: str) -> None:
        kw = keyword.strip()
        if not kw or not self.domain:
            return
        entry = self.site_memory.setdefault(self.domain, {})
        kws = entry.setdefault("working_keywords", [])
        if kw not in kws:
            kws.append(kw)
            entry["working_keywords"] = kws[-5:]
            _save_site_memory(self.site_memory)

    def remember_login_required(self, required: bool) -> None:
        if not self.domain:
            return
        entry = self.site_memory.setdefault(self.domain, {})
        entry["mandatory_login_seen"] = required
        _save_site_memory(self.site_memory)

    def render(self) -> str:
        lines = ["[AGENT WORKING MEMORY — ALWAYS UP TO DATE]"]
        domain_info = self.site_memory.get(self.domain, {})
        if domain_info.get("working_keywords"):
            lines.append(f"- Known useful keywords on {self.domain}: {', '.join(domain_info['working_keywords'])}")
        if self.saved_summaries:
            lines.append(f"- Already saved to CSV ({len(self.saved_summaries)} rows):")
            for idx, s in enumerate(self.saved_summaries[-8:], 1):
                lines.append(f"  {idx}. {s}")
        else:
            lines.append("- Already saved to CSV: 0 rows")

        if self.notes:
            lines.append("- Saved scratchpad notes:")
            for n in self.notes:
                lines.append(f"  * {n}")

        if self.pending_urls:
            lines.append("- Unvisited candidate URLs found on site (pick next one when ready):")
            for idx, u in enumerate(self.pending_urls[:8], 1):
                lines.append(f"  {idx}. {u}")

        if self.visited_urls:
            lines.append(f"- Already visited URLs ({len(self.visited_urls)}): {', '.join(u[:75] for u in self.visited_urls[-5:])}")
        if self.broken_urls:
            lines.append(f"- Broken/404 URLs (DO NOT retry): {', '.join(u[:75] for u in self.broken_urls[-4:])}")
        return "\n".join(lines)


def load_system_prompt(filepath: str) -> str:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return DEFAULT_SYSTEM_PROMPT


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

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _filter_kwargs(cls, kwargs: dict) -> dict:
    try:
        sig = inspect.signature(cls.__init__)
        return {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        return kwargs


def _resolve_spa_url(url: str, main_url: str = "") -> str:
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


async def _launch_persistent_browser(p, headless: bool):
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    common = dict(
        user_data_dir=USER_DATA_DIR,
        headless=headless,
        viewport={"width": 1366, "height": 850},
        ignore_default_args=["--enable-automation"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    for ch in ("chrome", "msedge", None):
        try:
            if ch:
                return await p.chromium.launch_persistent_context(channel=ch, **common)
            return await p.chromium.launch_persistent_context(**common)
        except Exception:
            continue
    raise RuntimeError("Could not launch Chromium/Chrome/Edge via Playwright.")


async def _extract_dom_markdown(page) -> str:
    try:
        await page.evaluate(DISMISS_POPUPS_JS)
        await page.wait_for_timeout(800)
    except Exception:
        pass

    try:
        for _ in range(4):
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.85)")
            await page.wait_for_timeout(450)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    raw = await page.evaluate("""
    () => {
        const clone = document.body.cloneNode(true);
        clone.querySelectorAll('nav, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());

        clone.querySelectorAll('a[href]').forEach(a => {
            const txt = (a.innerText || '').trim().replace(/\\s+/g, ' ');
            const href = a.href || '';
            if (txt && href && href.startsWith('http') && !href.includes('javascript:')) {
                a.replaceWith(document.createTextNode(`${txt} (${href}) `));
            }
        });
        return clone.innerText || '';
    }
    """)
    final_url = page.url or ""
    cleaned = clean_markdown(raw or "")
    if any(x in final_url.lower() for x in ("ap/signin", "/login", "/signin", "account/login", "/uas/login")):
        cleaned = f"[REDIRECTED TO LOGIN PAGE: {final_url}]\n" + cleaned
    return cleaned


async def _playwright_stealth_fetch(url: str) -> str:
    print(f"  [Fallback] Headless blocked/empty, retrying with headed browser for '{url[:60]}...'")
    async with async_playwright() as p:
        context = await _launch_persistent_browser(p, headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(4500)

            for _ in range(3):
                body_low = (await page.inner_text("body")).lower()
                if any(b in body_low for b in ("just a moment", "checking your browser")):
                    await page.wait_for_timeout(3000)
                else:
                    break

            return await _extract_dom_markdown(page)
        finally:
            await context.close()


def _is_404_page(text: str) -> bool:
    low = text.lower()
    total_len = len(text)
    if total_len < 12000 and any(e in low for e in ERROR_PHRASES):
        return True
    if any(e in low[:3500] for e in ERROR_PHRASES) and total_len < 18000:
        return True
    return False


def _is_bot_blocked(text: str) -> bool:
    if _is_404_page(text):
        return False
    low = text.lower()
    if len(text) < 350:
        return True
    if len(text) < 4000 and any(p in low for p in BOT_BLOCK_PHRASES):
        return True
    return False


def _is_mandatory_login_wall(text: str) -> bool:
    low = text.lower()
    total_len = len(text)
    head = low[:6000]

    if "[redirected to login page" in low[:500]:
        return True

    quora_hits = sum(1 for m in QUORA_GATE_MARKERS if m in low[:10000])
    quora_content_signals = sum(1 for s in ("upvote", "answered ", "credentials", "related questions", "follow") if s in low)
    if quora_hits >= 2 and quora_content_signals <= 1:
        return True

    if any(p in head for p in MANDATORY_LOGIN_PHRASES):
        return True
    if total_len < 6000 and any(p in low for p in MANDATORY_LOGIN_PHRASES):
        return True

    if total_len < 1200 and any(k in low for k in ("log in", "sign in", "sign up", "create account")):
        return True

    return False


# ---------------- TOOL 1: Smart Website Scraper (Session-Aware) ----------------
async def scrape_website(
    url: str,
    start_char: int = 0,
    keyword: str = "",
    seen_calls: set | None = None,
    main_url: str = "",
    memory: WorkingMemory | None = None,
) -> str:
    url = (url or "").strip()
    start_char = int(start_char or 0)
    keyword = (keyword or "").strip()

    if not url or not url.startswith(("http://", "https://")):
        return "Error: Provide a valid full URL starting with http:// or https://"

    url = _resolve_spa_url(url, main_url=main_url)
    cache_url = url.split("#")[0]

    call_key = (cache_url, start_char, keyword.lower())
    if seen_calls is not None:
        if call_key in seen_calls:
            print(f"  [Blocked Duplicate] {call_key[0][:60]}... (start={start_char}, kw='{keyword}')")
            return (
                "DUPLICATE CALL BLOCKED: You already checked this exact URL & parameters. "
                "Check [AGENT WORKING MEMORY] for unvisited URLs, use a NEW keyword (like 'Customer reviews', 'Ratings'), "
                "use a NEW start_char, or call save_to_csv."
            )
        seen_calls.add(call_key)

    print(f"  [Tool Running] scrape_website('{url}', start_char={start_char}, keyword='{keyword}')")

    if cache_url not in PAGE_CACHE:
        has_profile = os.path.exists(USER_DATA_DIR)
        browser_kwargs = _filter_kwargs(BrowserConfig, {
            "browser_type": "chromium",
            "headless": True,
            "enable_stealth": True,
            "chrome_channel": "chrome" if has_profile else "chromium",
            "use_persistent_context": has_profile,
            "user_data_dir": USER_DATA_DIR if has_profile else None,
            "viewport_width": 1366,
            "viewport_height": 850,
            "extra_args": ["--disable-blink-features=AutomationControlled"],
            "verbose": False,
        })
        browser_config = BrowserConfig(**browser_kwargs)

        run_kwargs = _filter_kwargs(CrawlerRunConfig, {
            "cache_mode": CacheMode.BYPASS,
            "markdown_generator": DefaultMarkdownGenerator(),
            "wait_until": "domcontentloaded",
            "delay_before_return_html": 2.5,
            "page_timeout": 60000,
            "scan_full_page": True,
            "scroll_delay": 0.35,
            "max_scroll_steps": 12,
            "remove_overlay_elements": True,
            "excluded_tags": ["nav", "footer", "script", "style", "noscript"],
            "simulate_user": True,
            "override_navigator": True,
            "magic": True,
            "js_code": DISMISS_POPUPS_JS,
        })
        run_config = CrawlerRunConfig(**run_kwargs)

        cleaned = ""
        crawl_ok = False
        error_msg = ""

        try:
            async with AsyncWebCrawler(config=browser_config) as crawler:
                result = await crawler.arun(url=cache_url, config=run_config)
                if result.success:
                    crawl_ok = True
                    final_url = getattr(result, "redirected_url", None) or getattr(result, "url", cache_url)
                    raw_md = result.markdown.raw_markdown if result.markdown and result.markdown.raw_markdown else ""
                    cleaned = clean_markdown(raw_md)
                    if any(x in str(final_url).lower() for x in ("ap/signin", "/login", "/signin", "account/login", "/uas/login")):
                        cleaned = f"[REDIRECTED TO LOGIN PAGE: {final_url}]\n" + cleaned
                else:
                    error_msg = result.error_message or "Crawl failed"
        except Exception as e:
            error_msg = str(e)

        if not crawl_ok or _is_bot_blocked(cleaned):
            try:
                fallback_text = await _playwright_stealth_fetch(cache_url)
                if len(fallback_text) > len(cleaned):
                    cleaned = fallback_text
                    crawl_ok = True
            except Exception as fe:
                if not crawl_ok:
                    return f"Error scraping {url}: {error_msg} | Fallback error: {fe}"

        PAGE_CACHE[cache_url] = cleaned
        preview = cleaned[:160].replace("\n", " ")
        print(f"  [Page Preview] {preview}...")

        if memory is not None and not _is_404_page(cleaned) and not _is_mandatory_login_wall(cleaned):
            memory.mark_visited(cache_url)
            memory.extract_candidate_urls(cleaned, cache_url)

    text = PAGE_CACHE[cache_url]
    total_len = len(text)
    print(f"  [Tool Output] Clean length: {total_len} chars")

    if _is_404_page(text):
        print(f"  [404 Detected] Broken/deleted page: {cache_url[:70]}...")
        if memory is not None:
            memory.mark_broken(cache_url)
        main_cache_url = main_url.split("#")[0] if main_url else ""
        if main_cache_url and main_cache_url != cache_url and main_cache_url in PAGE_CACHE:
            main_len = len(PAGE_CACHE[main_cache_url])
            return (
                f"[404 / BROKEN SUB-LINK: '{cache_url}' is deleted/moved (404). DO NOT retry it or call login on it! "
                f"Pick another URL from [AGENT WORKING MEMORY] or return to '{main_cache_url}' ({main_len} chars cached).]"
            )
        return (
            f"[404 / BROKEN PAGE: '{cache_url}' returned 'moved or deleted' / 'page not found'. "
            "Do NOT call login_or_signup_with_email on this URL!]"
        )

    if _is_mandatory_login_wall(text):
        print(f"  [Login Wall Detected] Mandatory login required on '{cache_url[:60]}...'")
        if memory is not None:
            memory.remember_login_required(True)
        return (
            f"[MANDATORY LOGIN WALL DETECTED on '{cache_url}'! "
            f"Do NOT browse further or guess data. Call login_or_signup_with_email(url='{cache_url}') NOW!]"
        )

    if total_len < 350:
        return f"[Warning: Page returned only {total_len} chars]\n{text}"

    if keyword:
        all_matches = [m.start() for m in re.finditer(re.escape(keyword), text, re.IGNORECASE)]
        if all_matches:
            if memory is not None:
                memory.remember_keyword(keyword)
            if start_char > 0:
                after = [m for m in all_matches if m >= start_char]
                target = after[0] if after else start_char
                s = max(0, target - 200)
                chunk = text[s : s + 3000]
                return f"[Total: {total_len} chars | Jumped to {s}..{s + len(chunk)} near '{keyword}']\n{chunk}"

            if len(all_matches) <= 4:
                chosen = all_matches
            else:
                chosen = sorted(set([
                    all_matches[0],
                    all_matches[len(all_matches) // 2],
                    all_matches[(3 * len(all_matches)) // 4],
                    all_matches[-1],
                ]))
            snippets = []
            for idx in chosen[:4]:
                s = max(0, idx - 200)
                snippets.append(f"--- Match at char {idx} ---\n" + text[s : s + 950])

            step_sz = max(1, len(all_matches) // 12)
            sampled = all_matches[::step_sz][:12]
            offsets_preview = ", ".join(str(m) for m in sampled)

            combined = "\n\n".join(snippets)[:3800]
            return (
                f"[Total: {total_len} chars | Keyword '{keyword}' matched {len(all_matches)} times (sample offsets: {offsets_preview})]\n"
                f"{combined}"
            )
        else:
            if start_char > 0 and start_char < total_len:
                chunk = text[start_char : start_char + 3000]
                return (
                    f"[Keyword '{keyword}' not found, showing text at requested start_char {start_char}..{start_char + len(chunk)}]\n{chunk}"
                )
            return (
                f"[Keyword '{keyword}' NOT FOUND anywhere in {total_len} chars on '{cache_url}'. "
                f"If you want to search the website for '{keyword}', call search_on_website(url='{cache_url}', query='{keyword}').]"
            )

    chunk = text[start_char : start_char + 3000]
    return f"[Total: {total_len} chars | Showing {start_char} to {start_char + len(chunk)}]\n{chunk}"


# ---------------- TOOL 2: Universal In-Site Search Helper ----------------
async def search_on_website(
    url: str, query: str, seen_calls: set | None = None, memory: WorkingMemory | None = None
) -> str:
    url = (url or "").strip()
    query = (query or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return "Error: Provide a valid full URL starting with http:// or https://"
    if not query:
        return "Error: Provide a non-empty search query."

    cache_url = url.split("#")[0]
    call_key = ("SEARCH", cache_url, query.lower())
    if seen_calls is not None:
        if call_key in seen_calls:
            return f"DUPLICATE SEARCH BLOCKED: Already searched '{query}' on '{cache_url}'."
        seen_calls.add(call_key)

    if cache_url in PAGE_CACHE and _is_mandatory_login_wall(PAGE_CACHE[cache_url]):
        return f"[MANDATORY LOGIN WALL DETECTED on '{cache_url}'. Call login_or_signup_with_email(url='{cache_url}') BEFORE searching!]"

    print(f"  [Tool Running] search_on_website('{url}', query='{query}')")

    async def _run_search(headless: bool) -> tuple[str, str, str]:
        async with async_playwright() as p:
            context = await _launch_persistent_browser(p, headless=headless)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(3500)

                pre_text = await _extract_dom_markdown(page)
                if _is_mandatory_login_wall(pre_text):
                    PAGE_CACHE[cache_url] = pre_text
                    return cache_url, pre_text, "LOGIN_WALL"

                try:
                    await page.evaluate(DISMISS_POPUPS_JS)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass

                search_selectors = [
                    "input[type='search']",
                    "input[placeholder*='search' i]",
                    "input[aria-label*='search' i]",
                    "input[name='q']",
                    "input[name*='search' i]",
                    "input[name*='query' i]",
                    "input[id*='search' i]",
                    "input[class*='search' i]",
                ]

                async def _find_visible_input():
                    for sel in search_selectors:
                        try:
                            loc = page.locator(sel).first
                            if await loc.is_visible(timeout=500):
                                return loc
                        except Exception:
                            pass
                    return None

                search_box = await _find_visible_input()
                if not search_box:
                    toggle_selectors = [
                        "button[aria-label*='search' i]",
                        "[role='button'][aria-label*='search' i]",
                        "a[aria-label*='search' i]",
                        "[data-testid*='search' i]",
                    ]
                    for tsel in toggle_selectors:
                        try:
                            tloc = page.locator(tsel).first
                            if await tloc.is_visible(timeout=500):
                                await tloc.click()
                                await page.wait_for_timeout(1000)
                                search_box = await _find_visible_input()
                                if search_box:
                                    break
                        except Exception:
                            pass

                if not search_box:
                    return cache_url, pre_text, "NO_SEARCH_BOX"

                await search_box.click()
                await search_box.fill(query)
                await page.wait_for_timeout(500)
                await search_box.press("Enter")
                await page.wait_for_timeout(4500)

                res_url = (page.url or cache_url).split("#")[0]
                res_text = await _extract_dom_markdown(page)
                return res_url, res_text, "OK"
            finally:
                await context.close()

    res_url, res_text, status = await _run_search(headless=True)
    if status != "LOGIN_WALL" and (status == "NO_SEARCH_BOX" or _is_bot_blocked(res_text)):
        print("  [Search Fallback] Retrying search with headed browser...")
        res_url, res_text, status = await _run_search(headless=False)

    if status == "LOGIN_WALL" or _is_mandatory_login_wall(res_text):
        PAGE_CACHE[res_url] = res_text
        if memory is not None:
            memory.remember_login_required(True)
        print(f"  [Login Wall Detected] Mandatory login required on '{res_url[:60]}...'")
        return f"[MANDATORY LOGIN WALL DETECTED on '{res_url}'! Call login_or_signup_with_email(url='{res_url}') NOW!]"

    if status == "NO_SEARCH_BOX":
        return f"Could not locate a visible search bar on '{url}'. Try scraping a direct category/search URL or read '{url}'."

    PAGE_CACHE[res_url] = res_text
    if memory is not None:
        memory.mark_visited(res_url)
        memory.extract_candidate_urls(res_text, res_url)
    print(f"  [Search Complete] '{res_url}' ({len(res_text)} chars)")
    return (
        f"[Search Results URL: {res_url} | Total: {len(res_text)} chars | Showing 0 to {min(3000, len(res_text))}]\n"
        f"{res_text[:3000]}"
    )


# ---------------- TOOL 3: On-Demand Email Login / Sign-up Helper ----------------
async def login_or_signup_with_email(url: str, seen_calls: set) -> tuple[str, bool]:
    print(f"\n  [Tool Running] login_or_signup_with_email('{url}')")

    prompt_msg = (
        f"\n>>> [LOGIN REQUIRED] Mandatory login is required to scrape '{url}'.\n"
        f">>> Enter Login/Signup Email (press Enter for '{DEFAULT_LOGIN_EMAIL}', or type 'skip'): "
        if DEFAULT_LOGIN_EMAIL
        else f"\n>>> [LOGIN REQUIRED] Mandatory login is required to scrape '{url}'.\n"
             ">>> Enter Login/Signup Email (press Enter to skip): "
    )
    email = (await asyncio.to_thread(input, prompt_msg)).strip()

    if not email and DEFAULT_LOGIN_EMAIL:
        email = DEFAULT_LOGIN_EMAIL

    if not email or email.lower() == "skip":
        return (
            "User skipped login. Do NOT retry login; extract any publicly available data or clearly state that mandatory login was skipped.",
            True,
        )

    cache_url = url.split("#")[0]

    async with async_playwright() as p:
        context = await _launch_persistent_browser(p, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(3500)

        try:
            body_text = (await page.inner_text("body")).lower()
            if any(e in body_text for e in ERROR_PHRASES):
                parsed_u = urlparse(url)
                home_url = f"{parsed_u.scheme}://{parsed_u.netloc}/"
                print(f"  [Notice] '{url}' is a broken/404 page; opening homepage '{home_url}'...")
                await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
        except Exception:
            pass

        btn_selectors = [
            "button:has-text('Sign up with email')",
            "button:has-text('Continue with email')",
            "a:has-text('Sign up with email')",
            "div[role='button']:has-text('Sign up with email')",
            "button:has-text('Use email')",
            "a:has-text('Login')",
            "button:has-text('Login')",
            "button:has-text('Log in')",
            "a:has-text('Sign in')",
            "button:has-text('Sign in')",
        ]
        for sel in btn_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=500):
                    await loc.click()
                    await page.wait_for_timeout(1200)
                    print(f"  [Auto-Clicked] '{sel}'")
                    break
            except Exception:
                pass

        email_selectors = [
            "input[type='email']",
            "input[name*='email' i]",
            "input[id*='email' i]",
            "input[placeholder*='email' i]",
        ]
        filled = False
        for sel in email_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=800):
                    await loc.fill(email)
                    filled = True
                    print(f"  [Auto-Filled] Email '{email}' entered!")
                    break
            except Exception:
                pass

        if not filled:
            print("  [Notice] Email field was not auto-detected — please enter email manually in the browser.")

        print("\n>>> BROWSER WINDOW IS OPEN <<<")
        print(">>> Check Email/Mobile, and complete Password / OTP / CAPTCHA to log in.")
        await asyncio.to_thread(input, ">>> Press ENTER here after completing Login/Signup: ")

        PAGE_CACHE.clear()
        seen_calls.clear()
        try:
            active_page = context.pages[-1] if context.pages else page
            if cache_url.rstrip("/") not in (active_page.url or "").rstrip("/"):
                await active_page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await active_page.wait_for_timeout(3500)
            unlocked_text = await _extract_dom_markdown(active_page)
            if len(unlocked_text) > 350 and not _is_mandatory_login_wall(unlocked_text):
                PAGE_CACHE[cache_url] = unlocked_text
                print(f"  [Session Captured] Unlocked page cached ({len(unlocked_text)} chars).")
        except Exception as e:
            print(f"  [Notice] Post-login direct capture skipped ({e}); will re-scrape via saved profile.")

        await context.close()

    return (
        f"Login/Signup completed with {email} and session saved. Now call scrape_website or search_on_website on '{cache_url}'.",
        False,
    )


# ---------------- TOOL 4: Validated Incremental CSV Saver ----------------
def _is_placeholder(val) -> bool:
    v = str(val).strip().lower()
    if v in {"", "n/a", "na", "none", "unknown", "null", "-"}:
        return True
    blocked_phrases = (
        "not publicly visible",
        "requires login",
        "login required",
        "sign in required",
        "requires authentication",
        "not available",
    )
    return any(p in v for p in blocked_phrases)


def save_to_csv(
    filename: str,
    items: list[dict],
    append: bool = True,
    login_used: bool = False,
    login_skipped: bool = False,
    memory: WorkingMemory | None = None,
) -> str:
    print(f"  [Tool Running] save_to_csv('{filename}', {len(items) if isinstance(items, list) else 0} items, append={append})")
    if not items or not isinstance(items, list):
        return "Error: items must be a non-empty list of objects."

    for item in items:
        if isinstance(item, dict):
            for v in item.values():
                s = str(v).strip().lower()
                if any(p in s for p in ("requires login", "login required", "sign in required", "requires authentication", "not publicly visible")):
                    if not login_used and not login_skipped:
                        return (
                            "REJECTED: You wrote placeholder text like 'Not publicly visible' / 'requires login'. "
                            "Do NOT save placeholders! If login is required, call login_or_signup_with_email(url) NOW. "
                            "Otherwise, read the page using scrape_website or search_on_website."
                        )

    valid_items = [
        item for item in items
        if isinstance(item, dict)
        and any(not _is_placeholder(v) for v in item.values())
    ]
    if not valid_items:
        if login_skipped:
            return "NO DATA SAVED: Mandatory login was skipped by the user and no public fields were found. Finish and inform the user."
        return (
            "REJECTED: Every field was empty or placeholder. "
            "If mandatory login is needed, call login_or_signup_with_email(url). Only save rows with real extracted values."
        )

    full_path = resolve_csv_path(filename)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    file_exists = os.path.exists(full_path) and os.path.getsize(full_path) > 0

    keys = []
    if append and file_exists:
        with open(full_path, mode="r", newline="", encoding="utf-8-sig") as rf:
            keys = next(csv.reader(rf), [])

    if append and file_exists and keys:
        mode = "a"
        write_header = False
    else:
        keys = []
        for item in valid_items:
            for k in item.keys():
                if k not in keys:
                    keys.append(k)
        mode = "w"
        write_header = True

    with open(full_path, mode=mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(valid_items)

    if memory is not None:
        memory.record_saved_rows(valid_items)

    print(f"  [CSV Saved] {len(valid_items)} valid rows -> {full_path}")
    for row in valid_items:
        print(f"    -> {row}")
    return f"Saved {len(valid_items)} real rows to {full_path}. Check [AGENT WORKING MEMORY] for remaining unvisited URLs."


def build_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "scrape_website",
                "description": (
                    "Fetch clean text and links from a URL. Use 'keyword' (like 'Customer reviews', 'Ratings', 'Price') "
                    "to jump straight to sections on large pages, or 'start_char' with keyword='' to browse offsets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full URL starting with https://"},
                        "start_char": {"type": "integer", "description": "Offset to read from (default 0)"},
                        "keyword": {"type": "string", "description": "Optional real phrase to jump to on the page (leave empty when browsing offsets)"},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_on_website",
                "description": (
                    "Search for a topic, article, or product using the website's own search bar. "
                    "Use this when the requested topic/items are not visible on a homepage."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The website URL (e.g. homepage) to search on"},
                        "query": {"type": "string", "description": "Search query (e.g. 'Machine Learning')"},
                    },
                    "required": ["url", "query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_memory_note",
                "description": (
                    "Save partial extracted facts (e.g., product model, price, rating) into working memory "
                    "while you search further down the page for remaining fields (e.g., review, reviewer_name)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "Concise fact note to keep in working memory"}
                    },
                    "required": ["note"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_to_csv",
                "description": "Save extracted items into CSV immediately after finding each item.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "description": "CSV filename"},
                        "items": {
                            "type": "array",
                            "description": "List of objects with ONLY the real values extracted for the user prompt.",
                            "items": {"type": "object"},
                        },
                        "append": {"type": "boolean", "description": "True to append, false to overwrite"},
                    },
                    "required": ["filename", "items"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "login_or_signup_with_email",
                "description": (
                    "ONLY call this if scrape_website or search_on_website reports [MANDATORY LOGIN WALL DETECTED] or redirects to sign-in. "
                    "NEVER call this if public data is already visible or for a 404 page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The login/signup or blocked page URL"}
                    },
                    "required": ["url"],
                },
            },
        },
    ]


def trim_messages(messages: list, memory: WorkingMemory | None = None) -> list:
    safe = list(messages)
    if memory is not None and safe and isinstance(safe[0], dict) and safe[0].get("role") == "system":
        safe[0] = {
            "role": "system",
            "content": safe[0]["content"] + "\n\n" + memory.render(),
        }

    if len(safe) <= 9:
        return safe

    trimmed = []
    cutoff = len(safe) - 6
    for i, msg in enumerate(safe):
        if 2 <= i < cutoff and isinstance(msg, dict) and msg.get("role") == "tool" and len(msg.get("content", "")) > 450:
            content = msg.get("content", "")
            compact = {
                "role": "tool",
                "content": content[:220] + "\n...[Older output trimmed — see [AGENT WORKING MEMORY] above for saved notes, URLs & CSV progress.]",
            }
            if "tool_call_id" in msg:
                compact["tool_call_id"] = msg["tool_call_id"]
            if "tool_name" in msg:
                compact["tool_name"] = msg["tool_name"]
            trimmed.append(compact)
        else:
            trimmed.append(msg)
    return trimmed


# ---------------- UNIFIED LLM CALLER (OLLAMA / GROQ) ----------------
def call_llm(safe_messages: list, tools: list, step: int):
    global CURRENT_GROQ_IDX

    if LLM_PROVIDER == "ollama":
        response = ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=safe_messages,
            tools=tools,
            think=False,
            options={"num_ctx": 12288, "temperature": 0.0},
        )
        msg = response.message
        normalized_calls = []
        for idx, tc in enumerate(msg.tool_calls or []):
            raw_args = tc.function.arguments
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    parsed = json.loads(raw_args)
                    args = parsed if isinstance(parsed, dict) else {"_parse_error": "Tool arguments must be a JSON object."}
                except json.JSONDecodeError:
                    args = {"_parse_error": f"Invalid JSON arguments: {raw_args}"}
            normalized_calls.append((f"ollama_{step}_{idx}", tc.function.name, args))
        return msg, (msg.content or ""), normalized_calls

    for _ in range(len(GROQ_FALLBACK_MODELS) + 2):
        model_name = GROQ_FALLBACK_MODELS[CURRENT_GROQ_IDX % len(GROQ_FALLBACK_MODELS)]
        try:
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=safe_messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.0,
            )
            msg = response.choices[0].message
            history_msg = {"role": "assistant", "content": msg.content or ""}
            normalized_calls = []
            if msg.tool_calls:
                history_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
                for tc in msg.tool_calls:
                    try:
                        parsed = json.loads(tc.function.arguments)
                        args = parsed if isinstance(parsed, dict) else {"_parse_error": "Tool arguments must be a JSON object."}
                    except json.JSONDecodeError:
                        args = {"_parse_error": f"Invalid JSON arguments: {tc.function.arguments}"}
                    normalized_calls.append((tc.id, tc.function.name, args))
            return history_msg, (msg.content or ""), normalized_calls
        except APIStatusError as e:
            if e.status_code == 429 and "tokens per day" in str(e).lower():
                print(f"  [TPD Full on {model_name}] Switching to next free Groq model...")
                CURRENT_GROQ_IDX += 1
            elif e.status_code in (413, 429):
                print(f"  [Rate Limit {e.status_code} on {model_name}] Waiting 20s...")
                time.sleep(20)
            else:
                raise
    raise RuntimeError("All Groq fallback models hit rate limits. Switch LLM_PROVIDER=ollama in .env!")


# ---------------- ASLI AGENT LOOP ----------------
async def run_agent(url: str, user_goal: str, csv_filename: str, max_steps: int = 30):
    csv_filename = resolve_csv_path(csv_filename)
    PAGE_CACHE.clear()
    seen_calls: set[tuple] = set()
    memory = WorkingMemory(url)
    system_prompt = load_system_prompt(SYSTEM_PROMPT_FILE)
    tools = build_tools()

    active_model = OLLAMA_MODEL if LLM_PROVIDER == "ollama" else GROQ_FALLBACK_MODELS[CURRENT_GROQ_IDX % len(GROQ_FALLBACK_MODELS)]
    print(f"\n[Agent Started] Provider: {LLM_PROVIDER.upper()} | Model: {active_model} | Output: {csv_filename}")

    full_prompt = (
        f"Target URL: {url}\n"
        f"User Requirement (extract ONLY what is asked): {user_goal}\n"
        f"Save extracted records into CSV file: '{csv_filename}'.\n"
        "First call scrape_website on the Target URL. If the Target URL is a homepage and the requested topic/items are not there, "
        "call search_on_website. Use save_memory_note to hold partial facts while reading large pages, "
        "use keyword (like 'Customer reviews' or 'Ratings') to jump straight to reviews, and call save_to_csv after EACH completed item."
    )

    messages: list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": full_prompt},
    ]

    saved_csv = False
    nudge_count = 0
    consecutive_dups = 0
    login_used = False
    login_skipped = False
    finished = False
    step = 0

    for step in range(1, max_steps + 1):
        current_model = OLLAMA_MODEL if LLM_PROVIDER == "ollama" else GROQ_FALLBACK_MODELS[CURRENT_GROQ_IDX % len(GROQ_FALLBACK_MODELS)]
        print(f"\n--- Agent Step {step} ({current_model}) ---")
        safe_messages = trim_messages(messages, memory=memory)
        history_msg, text_content, tool_calls = call_llm(safe_messages, tools, step)
        messages.append(history_msg)

        if not tool_calls:
            if not saved_csv and not login_skipped and nudge_count < 2:
                nudge_count += 1
                print(f"  [Guardrail] '{csv_filename}' has not been created yet; prompting agent to save ({nudge_count}/2)...")
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have NOT called save_to_csv yet and '{csv_filename}' was not created. "
                        f"Check [AGENT WORKING MEMORY] for unvisited URLs and notes. "
                        f"If you found valid items, call save_to_csv(filename='{csv_filename}', items=[...], append=False) NOW."
                    ),
                })
                continue

            print("\nAgent Final Response:\n", text_content)
            finished = True
            break

        for tc_id, fn_name, args in tool_calls:
            if "_parse_error" in args:
                tool_output = args["_parse_error"]
            elif fn_name == "scrape_website":
                tool_output = await scrape_website(
                    url=args.get("url") or "",
                    start_char=args.get("start_char") or 0,
                    keyword=args.get("keyword") or "",
                    seen_calls=seen_calls,
                    main_url=url,
                    memory=memory,
                )
                if "DUPLICATE CALL BLOCKED" in str(tool_output):
                    consecutive_dups += 1
                else:
                    consecutive_dups = 0
            elif fn_name == "search_on_website":
                tool_output = await search_on_website(
                    url=args.get("url") or url,
                    query=args.get("query") or "",
                    seen_calls=seen_calls,
                    memory=memory,
                )
                consecutive_dups = 0
            elif fn_name == "save_memory_note":
                tool_output = memory.add_note(args.get("note") or "")
                print(f"  [Memory Note] {args.get('note', '')[:80]}")
                consecutive_dups = 0
            elif fn_name == "login_or_signup_with_email":
                target_login_url = (args.get("url") or url).split("#")[0]
                cached_page = PAGE_CACHE.get(target_login_url, "")

                if login_used or login_skipped:
                    tool_output = "Login was already handled in this run. Check [AGENT WORKING MEMORY] and continue."
                elif cached_page and _is_404_page(cached_page):
                    print(f"  [Blocked Login on 404] '{target_login_url[:60]}...' is a broken page; skipping login request.")
                    tool_output = (
                        f"BLOCKED: '{target_login_url}' is a 404 / 'moved or deleted' page, NOT a login wall. "
                        "Pick another valid URL from [AGENT WORKING MEMORY]."
                    )
                elif cached_page and not _is_mandatory_login_wall(cached_page) and len(cached_page) >= 3500:
                    print(f"  [Skipped Unnecessary Login] Public content already visible ({len(cached_page)} chars).")
                    tool_output = (
                        "BLOCKED: Mandatory login is NOT required on this page—public content is already available! "
                        "Use scrape_website(keyword='Customer reviews') or scrape_website(keyword='Ratings') to jump straight to reviews."
                    )
                else:
                    tool_output, was_skipped = await login_or_signup_with_email(
                        url=args.get("url") or url,
                        seen_calls=seen_calls,
                    )
                    if was_skipped:
                        login_skipped = True
                    else:
                        login_used = True
                    consecutive_dups = 0
            elif fn_name == "save_to_csv":
                append_mode = bool(args.get("append", True)) if saved_csv else False
                tool_output = save_to_csv(
                    filename=csv_filename,
                    items=args.get("items", []),
                    append=append_mode,
                    login_used=login_used,
                    login_skipped=login_skipped,
                    memory=memory,
                )
                if str(tool_output).startswith("Saved "):
                    saved_csv = True
            else:
                tool_output = f"Unknown tool: {fn_name}"

            tool_msg = {"role": "tool", "content": str(tool_output)}
            if LLM_PROVIDER == "ollama":
                tool_msg["tool_name"] = fn_name
            else:
                tool_msg["tool_call_id"] = tc_id
            messages.append(tool_msg)

        if consecutive_dups >= 2:
            if not saved_csv and nudge_count < 2:
                nudge_count += 1
                print("\n[Loop Breaker] Duplicate scraping prevented. Directing agent to use working memory...")
                messages.append({
                    "role": "user",
                    "content": (
                        "Stop repeating the same scrape_website call. "
                        "Pick an unvisited URL from [AGENT WORKING MEMORY], or try keyword='Customer reviews' / 'Ratings', "
                        f"or call save_to_csv(filename='{csv_filename}', items=[...], append=False) with valid items you already found."
                    ),
                })
                consecutive_dups = 0
            else:
                print("\n[Stopped] Agent repeated duplicate URLs. Exiting cleanly.")
                break

        if LLM_PROVIDER == "groq":
            await asyncio.sleep(3)

    if not finished:
        print(f"\n--- Finished after {step} steps ---")
        if saved_csv and os.path.exists(csv_filename):
            print(f"Saved file ready: {os.path.abspath(csv_filename)}")
        else:
            print("No CSV file was created (requested fields were not publicly available).")


if __name__ == "__main__":
    print("=== Universal AI Web Scraping Agent ===")
    target_url = input("Paste Website URL: ").strip()
    user_prompt = input("What do you want to extract? ").strip()
    csv_file = input("Output CSV filename (saved in 'data/', Enter for 'output.csv'): ").strip() or "output.csv"

    asyncio.run(run_agent(target_url, user_prompt, csv_file))