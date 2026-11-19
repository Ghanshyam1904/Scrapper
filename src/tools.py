import asyncio
import csv
import os
import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from src.config import (
    USER_DATA_DIR,
    DEFAULT_LOGIN_EMAIL,
    PAGE_CACHE,
    ERROR_PHRASES,
    DISMISS_POPUPS_JS,
    resolve_csv_path,
)
from src.memory import WorkingMemory
from src.browser import (
    clean_markdown,
    filter_kwargs,
    resolve_spa_url,
    launch_persistent_browser,
    extract_dom_markdown,
    playwright_stealth_fetch,
    is_404_page,
    is_bot_blocked,
    is_mandatory_login_wall,
)


# ---------------- TOOL 1: Smart Website Scraper ----------------
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

    url = resolve_spa_url(url, main_url=main_url)
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
        browser_kwargs = filter_kwargs(BrowserConfig, {
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

        run_kwargs = filter_kwargs(CrawlerRunConfig, {
            "cache_mode": CacheMode.BYPASS,
            "markdown_generator": DefaultMarkdownGenerator(),
            "wait_until": "domcontentloaded",
            "delay_before_return_html": 2.5,
            "page_timeout": 60000,
            "scan_full_page": True,
            "scroll_delay": 0.35,
            "max_scroll_steps": 12,
            "remove_overlay_elements": True,
            "excluded_tags": ["nav", "aside", "footer", "script", "style", "noscript"],
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
                    if any(x in str(final_url).lower() for x in ("ap/signin", "/login", "/signin", "account/login", "/uas/login", "/authwall", "/checkpoint/")):
                        cleaned = f"[REDIRECTED TO LOGIN PAGE: {final_url}]\n" + cleaned
                else:
                    error_msg = result.error_message or "Crawl failed"
        except Exception as e:
            error_msg = str(e)

        if not crawl_ok or is_bot_blocked(cleaned):
            try:
                fallback_text = await playwright_stealth_fetch(cache_url)
                if len(fallback_text) > len(cleaned):
                    cleaned = fallback_text
                    crawl_ok = True
            except Exception as fe:
                if not crawl_ok:
                    return f"Error scraping {url}: {error_msg} | Fallback error: {fe}"

        PAGE_CACHE[cache_url] = cleaned
        preview = cleaned[:160].replace("\n", " ")
        print(f"  [Page Preview] {preview}...")

        if memory is not None and not is_404_page(cleaned) and not is_mandatory_login_wall(cleaned):
            memory.mark_visited(cache_url)
            memory.extract_candidate_urls(cleaned, cache_url)

    text = PAGE_CACHE[cache_url]
    total_len = len(text)
    print(f"  [Tool Output] Clean length: {total_len} chars")

    if is_404_page(text):
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

    if is_mandatory_login_wall(text):
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
    next_off = start_char + len(chunk)
    more_hint = (
        f" — Call start_char={next_off} on THIS URL if items are further down before visiting other links."
        if next_off < total_len
        else ""
    )
    return f"[Total: {total_len} chars | Showing {start_char} to {next_off}{more_hint}]\n{chunk}"


# ---------------- TOOL 2: Universal In-Site Search (With Section, Location & Filters) ----------------
async def search_on_website(
    url: str,
    query: str,
    location: str = "",
    section: str = "",
    filters: list[str] | None = None,
    seen_calls: set | None = None,
    memory: WorkingMemory | None = None,
) -> str:
    url = (url or "").strip()
    query = (query or "").strip()
    location = (location or "").strip()
    section = (section or "").strip()
    filters = [str(f).strip() for f in (filters or []) if str(f).strip()]

    if not url or not url.startswith(("http://", "https://")):
        return "Error: Provide a valid full URL starting with http:// or https://"
    if not query:
        return "Error: Provide a non-empty search query."

    cache_url = url.split("#")[0]
    call_key = ("SEARCH", cache_url, query.lower(), location.lower(), section.lower(), tuple(f.lower() for f in filters))
    if seen_calls is not None:
        if call_key in seen_calls:
            return f"DUPLICATE SEARCH BLOCKED: Already searched '{query}' on '{cache_url}'."
        seen_calls.add(call_key)

    print(f"  [Tool Running] search_on_website('{url}', query='{query}', location='{location}', section='{section}', filters={filters})")

    def _expand_filter_terms(label: str) -> list[str]:
        low = label.lower()
        if "24" in low:
            return ["Past 24 hours", "Last 24 hours", "24 hours", label]
        if "entry" in low:
            return ["Entry level", "Entry-level", label]
        return [label]

    async def _run_search(headless: bool) -> tuple[str, str, str]:
        async with async_playwright() as p:
            context = await launch_persistent_browser(p, headless=headless)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(3500)

                try:
                    await page.evaluate(DISMISS_POPUPS_JS)
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

                target_section = section or ("Jobs" if "linkedin.com" in url.lower() and "/jobs" not in url.lower() else "")
                if target_section:
                    for s_sel in (
                        f"a:has-text('{target_section}')",
                        f"button:has-text('{target_section}')",
                        f"[aria-label*='{target_section}' i]",
                    ):
                        try:
                            sloc = page.locator(s_sel).first
                            if await sloc.is_visible(timeout=800):
                                await sloc.click()
                                await page.wait_for_timeout(3500)
                                print(f"  [Section Opened] Clicked '{target_section}'")
                                break
                        except Exception:
                            pass

                pre_text = await extract_dom_markdown(page)
                if is_mandatory_login_wall(pre_text):
                    cur = (page.url or cache_url).split("#")[0]
                    PAGE_CACHE[cur] = pre_text
                    return cur, pre_text, "LOGIN_WALL"

                search_selectors = [
                    "input[aria-label*='Search by title' i]",
                    "input[placeholder*='Search job' i]",
                    "input[name='keywords']",
                    "input[type='search']",
                    "input[placeholder*='search' i]",
                    "input[aria-label*='search' i]",
                    "input[name='q']",
                    "input[name*='search' i]",
                    "input[name*='query' i]",
                    "input[id*='search' i]",
                ]

                async def _find_visible_input(sels):
                    for sel in sels:
                        try:
                            loc = page.locator(sel).first
                            if await loc.is_visible(timeout=500):
                                return loc
                        except Exception:
                            pass
                    return None

                search_box = await _find_visible_input(search_selectors)
                if not search_box:
                    for tsel in (
                        "button[aria-label*='search' i]",
                        "[role='button'][aria-label*='search' i]",
                        "a[aria-label*='search' i]",
                        "[data-testid*='search' i]",
                    ):
                        try:
                            tloc = page.locator(tsel).first
                            if await tloc.is_visible(timeout=500):
                                await tloc.click()
                                await page.wait_for_timeout(1000)
                                search_box = await _find_visible_input(search_selectors)
                                if search_box:
                                    break
                        except Exception:
                            pass

                if not search_box:
                    return cache_url, pre_text, "NO_SEARCH_BOX"

                await search_box.click()
                await search_box.fill(query)
                await page.wait_for_timeout(500)

                if location:
                    loc_box = await _find_visible_input([
                        "input[aria-label*='City, state' i]",
                        "input[aria-label*='location' i]",
                        "input[name='location']",
                        "input[placeholder*='location' i]",
                        "input[placeholder*='city' i]",
                    ])
                    if loc_box:
                        await loc_box.click()
                        await loc_box.fill("")
                        await loc_box.fill(location)
                        await page.wait_for_timeout(700)
                        await loc_box.press("Enter")
                    else:
                        await search_box.press("Enter")
                else:
                    await search_box.press("Enter")

                await page.wait_for_timeout(4500)

                if filters:
                    dropdown_triggers = ["Date posted", "Any time", "Experience level", "All filters", "Filter", "Sort by"]
                    for f_label in filters:
                        terms = _expand_filter_terms(f_label)
                        applied = False

                        for term in terms:
                            for fsel in (f"label:has-text('{term}')", f"button:has-text('{term}')", f"span:has-text('{term}')"):
                                try:
                                    floc = page.locator(fsel).first
                                    if await floc.is_visible(timeout=400):
                                        await floc.click()
                                        await page.wait_for_timeout(800)
                                        applied = True
                                        print(f"  [Filter Clicked] '{term}'")
                                        break
                                except Exception:
                                    pass
                            if applied:
                                break

                        if not applied:
                            for trig in dropdown_triggers:
                                try:
                                    tbtn = page.locator(f"button:has-text('{trig}')").first
                                    if await tbtn.is_visible(timeout=400):
                                        await tbtn.click()
                                        await page.wait_for_timeout(800)
                                        for term in terms:
                                            for osel in (f"label:has-text('{term}')", f"span:has-text('{term}')", f"button:has-text('{term}')"):
                                                try:
                                                    oloc = page.locator(osel).first
                                                    if await oloc.is_visible(timeout=400):
                                                        await oloc.click()
                                                        await page.wait_for_timeout(600)
                                                        applied = True
                                                        print(f"  [Filter Applied] '{trig}' -> '{term}'")
                                                        break
                                                except Exception:
                                                    pass
                                            if applied:
                                                break
                                        for abtn in ("Show results", "Apply", "Done"):
                                            try:
                                                aloc = page.locator(f"button:has-text('{abtn}')").first
                                                if await aloc.is_visible(timeout=400):
                                                    await aloc.click()
                                                    await page.wait_for_timeout(1500)
                                                    break
                                            except Exception:
                                                pass
                                        if applied:
                                            break
                                except Exception:
                                    pass
                    await page.wait_for_timeout(2500)

                res_url = (page.url or cache_url).split("#")[0]
                res_text = await extract_dom_markdown(page)
                return res_url, res_text, "OK"
            finally:
                await context.close()

    res_url, res_text, status = await _run_search(headless=True)
    if status != "LOGIN_WALL" and (status == "NO_SEARCH_BOX" or is_bot_blocked(res_text)):
        print("  [Search Fallback] Retrying search with headed browser...")
        res_url, res_text, status = await _run_search(headless=False)

    if status == "LOGIN_WALL" or is_mandatory_login_wall(res_text):
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


# ---------------- TOOL 3: On-Demand Email Login / Sign-up ----------------
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
        context = await launch_persistent_browser(p, headless=False)
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
            print("  [Notice] Email field not auto-detected — enter your email manually in the browser window.")

        print("\n>>> BROWSER WINDOW IS OPEN <<<")
        print(">>> Complete Email/Mobile, Password / OTP / CAPTCHA login inside the browser.")
        await asyncio.to_thread(input, ">>> Press ENTER here after Login/Signup is complete: ")

        PAGE_CACHE.clear()
        seen_calls.clear()
        try:
            active_page = context.pages[-1] if context.pages else page
            if cache_url.rstrip("/") not in (active_page.url or "").rstrip("/"):
                await active_page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await active_page.wait_for_timeout(3500)
            unlocked_text = await extract_dom_markdown(active_page)
            if len(unlocked_text) > 350 and not is_mandatory_login_wall(unlocked_text):
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