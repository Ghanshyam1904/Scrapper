import inspect
import os
import re
from playwright.async_api import async_playwright
from src.config import (
    USER_DATA_DIR,
    PAGE_CACHE,
    ERROR_PHRASES,
    BOT_BLOCK_PHRASES,
    MANDATORY_LOGIN_PHRASES,
    QUORA_GATE_MARKERS,
    LINKEDIN_GATE_MARKERS,
    DISMISS_POPUPS_JS,
)


def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"data:image/[^\s)]+", "", text)
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)

    # Shrink giant Amazon tracking URLs while keeping /dp/ASIN intact
    text = re.sub(
        r"(https?://(?:www\.)?amazon\.[a-z.]+/)(?:[^)\s\"']*/)?dp/([A-Z0-9]{10})[^)\s\"']*",
        r"\1dp/\2",
        text,
    )
    # Shrink giant Flipkart tracking URLs while keeping ?pid=...
    text = re.sub(
        r"(https?://(?:www\.)?flipkart\.com/[^)\s\"']+/p/itm[a-zA-Z0-9]+)\?[^)\s\"']*?\b(pid=[A-Za-z0-9]+)[^)\s\"']*",
        r"\1?\2",
        text,
    )
    # Shrink giant LinkedIn job tracking URLs while keeping /jobs/view/...
    text = re.sub(
        r"(https?://(?:[a-z]+\.)?linkedin\.com/jobs/view/[^?\s)\"']+)\?[^)\s\"']+",
        r"\1",
        text,
    )

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


async def launch_persistent_browser(p, headless: bool):
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


async def extract_dom_markdown(page) -> str:
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
        clone.querySelectorAll('nav, aside, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());

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
    if any(x in final_url.lower() for x in ("ap/signin", "/login", "/signin", "account/login", "/uas/login", "/authwall", "/checkpoint/")):
        cleaned = f"[REDIRECTED TO LOGIN PAGE: {final_url}]\n" + cleaned
    return cleaned


async def playwright_stealth_fetch(url: str) -> str:
    print(f"  [Fallback] Headless blocked/empty, retrying with headed browser for '{url[:60]}...'")
    async with async_playwright() as p:
        context = await launch_persistent_browser(p, headless=False)
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

            return await extract_dom_markdown(page)
        finally:
            await context.close()


def is_404_page(text: str) -> bool:
    low = text.lower()
    total_len = len(text)
    if total_len < 12000 and any(e in low for e in ERROR_PHRASES):
        return True
    if any(e in low[:3500] for e in ERROR_PHRASES) and total_len < 18000:
        return True
    return False


def is_bot_blocked(text: str) -> bool:
    if is_404_page(text):
        return False
    low = text.lower()
    if len(text) < 350:
        return True
    if len(text) < 4000 and any(p in low for p in BOT_BLOCK_PHRASES):
        return True
    return False


def is_mandatory_login_wall(text: str) -> bool:
    low = text.lower()
    total_len = len(text)
    head = low[:6000]

    if "[redirected to login page" in low[:500]:
        return True

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

    if total_len < 1200 and any(k in low for k in ("log in", "sign in", "sign up", "create account")):
        return True

    return False