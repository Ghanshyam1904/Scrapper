import asyncio
import csv
import os
import re
from urllib.parse import urljoin, urlparse
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from src.config import (
    USER_DATA_DIR,
    DEFAULT_LOGIN_EMAIL,
    PROXY_URL,
    PAGE_CACHE,
    DOM_SAMPLE_CACHE,
    ERROR_PHRASES,
    DISMISS_POPUPS_JS,
    resolve_csv_path,
)
from src.memory import WorkingMemory
from src.browser import (
    BROWSER_MANAGER,
    SMART_SCROLL_JS,
    clean_markdown,
    extract_dom_sample,
    extract_structured_data,
    filter_kwargs,
    resolve_spa_url,
    extract_dom_markdown,
    playwright_stealth_fetch_full,
    handle_human_verification,
    is_404_page,
    is_bot_blocked,
    is_mandatory_login_wall,
    verify_authenticated_session,
)

_TEXT_COL_HINTS = (
    "country", "capital", "city", "state", "author", "reviewer",
    "company", "title", "job_title", "model_name", "book_title", "name",
)
_NUMERIC_COL_HINTS = (
    "population", "area", "km2", "sq_km", "price", "cost",
    "amount", "points", "salary", "followers", "upvotes",
    "wins", "losses", "goals",
)
_EXEMPT_WORDS = ("id", "code", "rank", "count", "num", "number", "year", "url", "link", "rating")
_ALLOWED_NUMERIC_STATUS = (
    "unavailable", "out of stock", "in stock", "coming soon",
    "free", "discontinued", "tbd", "not available", "varies",
)

_EXTRA_BOT_MARKERS = (
    "robot or human",
    "activate and hold",
    "confirm that you’re human",
    "confirm that you're human",
    "pardon our interruption",
    "additional verification required",
    "request blocked",
    "you have been blocked",
    "your ray id",
)


def _anti_bot_reason(text: str) -> str:
    if not text:
        return "empty_page"
    low = text.lower()
    if "[redirected to login page" in low[:500]:
        return ""
    if len(text) < 350 and not is_404_page(text) and not is_mandatory_login_wall(text):
        return f"too_short({len(text)} chars)"
    head = low[:6000]
    if len(text) < 12000:
        for m in _EXTRA_BOT_MARKERS:
            if m in head:
                return f"marker:{m}"
    if is_bot_blocked(text):
        return "bot_block_phrase"
    return ""


def _is_anti_bot_text(text: str) -> bool:
    return bool(_anti_bot_reason(text))

def _merge_scroll_snapshots(snapshots: list[str]) -> str:
    """Preserve items seen across virtualized scroll steps without duplicate blocks."""
    seen: set[str] = set()
    ordered: list[str] = []
    for snap in snapshots:
        for block in snap.split("\n\n"):
            norm = re.sub(r"\s+", " ", block).strip()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            ordered.append(block.strip())
    return "\n\n".join(ordered)


def _validate_selector_rows(rows: list[dict], field_selectors: dict[str, str]) -> str | None:
    if not rows or len(field_selectors) < 2:
        return None
    cols = list(field_selectors.keys())

    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c1, c2 = cols[i], cols[j]
            pairs = [
                (str(r.get(c1, "")).strip(), str(r.get(c2, "")).strip())
                for r in rows
                if str(r.get(c1, "")).strip() and str(r.get(c2, "")).strip()
            ]
            if len(pairs) >= 2:
                same_cnt = sum(1 for a, b in pairs if a == b)
                if same_cnt / len(pairs) >= 0.5:
                    return (
                        f"SELECTOR FAILED: Columns '{c1}' and '{c2}' extracted identical text ('{pairs[0][0][:45]}...'). "
                        "Use specific per-field selectors or fall back to scrape_website + save_to_csv."
                    )

    title_cols = [c for c in cols if any(k in c.lower() for k in ("title", "name", "heading")) and "seller" not in c.lower()]
    scalar_cols = [c for c in cols if any(k in c.lower() for k in ("price", "cost", "shipping", "condition", "rating", "seller", "author"))]

    for r in rows[:5]:
        for sc in scalar_cols:
            sval = str(r.get(sc, "")).strip()
            if not sval:
                continue
            if len(sval) > 140:
                return (
                    f"SELECTOR FAILED: Column '{sc}' captured too much text ({len(sval)} chars), "
                    "indicating it matched a container instead of the specific field."
                )
            for tc in title_cols:
                tval = str(r.get(tc, "")).strip()
                if tc != sc and len(tval) >= 15 and tval.lower() in sval.lower() and len(sval) > len(tval):
                    return (
                        f"SELECTOR FAILED: Column '{sc}' swallowed '{tc}' ('{sval[:55]}...'). "
                        "Fall back to scrape_website + save_to_csv."
                    )
    return None


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
            unread = memory.get_next_unread_hint() if memory else ""
            return (
                "DUPLICATE CALL BLOCKED: You already checked this exact URL & parameters. "
                f"{unread or 'Open an unvisited URL from [AGENT WORKING MEMORY], or call save_to_csv.'}"
            )
        seen_calls.add(call_key)

    print(f"  [Tool Running] scrape_website('{url}', start_char={start_char}, keyword='{keyword}')")

    if cache_url not in PAGE_CACHE:
        cleaned = ""
        raw_html = ""
        crawl_ok = False
        error_msg = ""

        # Reuse active headless browser or saved auth cookies after login closes
        if BROWSER_MANAGER.is_active() or BROWSER_MANAGER.has_auth_cookies():
            try:
                _, page = await BROWSER_MANAGER.get_page(headless=True)
                await page.goto(cache_url, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(1500)
                await handle_human_verification(page)
                raw_html = await page.content()
                cleaned = await extract_dom_markdown(page)
                crawl_ok = True
            except Exception as e:
                error_msg = str(e)
        else:
            has_profile = os.path.exists(USER_DATA_DIR)
            browser_kwargs = filter_kwargs(BrowserConfig, {
                "browser_type": "chromium",
                "headless": True,
                "enable_stealth": True,
                "chrome_channel": "chrome" if has_profile else "chromium",
                "use_persistent_context": has_profile,
                "user_data_dir": USER_DATA_DIR if has_profile else None,
                "proxy": PROXY_URL or None,
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
                "delay_before_return_html": 1.5,
                "page_timeout": 60000,
                "scan_full_page": False,
                "remove_overlay_elements": True,
                "excluded_tags": ["aside", "footer", "script", "style", "noscript"],
                "simulate_user": True,
                "override_navigator": True,
                "magic": True,
                "js_code": DISMISS_POPUPS_JS,
            })
            run_config = CrawlerRunConfig(**run_kwargs)

            try:
                async with AsyncWebCrawler(config=browser_config) as crawler:
                    result = await crawler.arun(url=cache_url, config=run_config)
                    if result.success:
                        crawl_ok = True
                        final_url = getattr(result, "redirected_url", None) or getattr(result, "url", cache_url)
                        raw_html = getattr(result, "html", "") or ""
                        raw_md = result.markdown.raw_markdown if result.markdown and result.markdown.raw_markdown else ""
                        cleaned = clean_markdown(raw_md)
                        if any(x in str(final_url).lower() for x in ("ap/signin", "/login", "/signin", "account/login", "/uas/login", "/authwall", "/checkpoint/")):
                            cleaned = f"[REDIRECTED TO LOGIN PAGE: {final_url}]\n" + cleaned
                    else:
                        error_msg = result.error_message or "Crawl failed"
            except Exception as e:
                error_msg = str(e)

        if not crawl_ok or _is_anti_bot_text(cleaned):
            try:
                fallback_text, fallback_html = await playwright_stealth_fetch_full(cache_url)
                if len(fallback_text) > len(cleaned) or not crawl_ok:
                    cleaned = fallback_text
                    if fallback_html:
                        raw_html = fallback_html
                    crawl_ok = True
            except Exception as fe:
                if not crawl_ok:
                    return f"Error scraping {url}: {error_msg} | Fallback error: {fe}"

        if raw_html and not _is_anti_bot_text(cleaned) and not is_mandatory_login_wall(cleaned):
            structured = extract_structured_data(raw_html)
            if structured:
                cleaned = f"{structured}\n\n{cleaned}"
            sample = extract_dom_sample(raw_html)
            if sample:
                DOM_SAMPLE_CACHE[cache_url] = sample
            if any(p in raw_html.lower() for p in ('class="pagination', "page-link", ">next<", 'aria-label="next"', "load more")):
                cleaned = (
                    f"[PAGINATION DETECTED: Clickable Next/page buttons exist on '{cache_url}'. "
                    f"Use extract_with_selectors(..., mode='ajax' or 'url_pagination') or ajax_paginate_and_scrape(url='{cache_url}', max_pages=6)!]\n" + cleaned
                )

        PAGE_CACHE[cache_url] = cleaned
        preview = cleaned[:160].replace("\n", " ")
        print(f"  [Page Preview] {preview}...")

        if memory is not None and not is_404_page(cleaned) and not _is_anti_bot_text(cleaned) and not is_mandatory_login_wall(cleaned):
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

    if _is_anti_bot_text(text):
        print(f"  [Anti-Bot Blocked] Bot/CAPTCHA challenge on '{cache_url[:60]}...'")
        return (
            f"[ANTI-BOT BLOCKED on '{cache_url}': Site presented a CAPTCHA / Cloudflare / PerimeterX check ({total_len} chars). "
            "Do NOT call login_or_signup_with_email! Try another URL from [AGENT WORKING MEMORY] or inform the user.]"
        )

    if is_mandatory_login_wall(text):
        print(f"  [Login Wall Detected] Mandatory login required on '{cache_url[:60]}...'")
        if memory is not None:
            memory.remember_login_required(True)
        return (
            f"[MANDATORY LOGIN WALL DETECTED on '{cache_url}'! "
            f"Call login_or_signup_with_email(url='{cache_url}') NOW. "
            "ONLY pass username/password if real demo credentials are literally printed in the page text below; NEVER guess 'admin' or fake passwords.]\n"
            f"{text[:1500]}"
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
                chunk = text[s : s + 5000]
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
                snippets.append(f"--- Match at char {idx} ---\n" + text[s : s + 1100])

            step_sz = max(1, len(all_matches) // 12)
            sampled = all_matches[::step_sz][:12]
            offsets_preview = ", ".join(str(m) for m in sampled)

            combined = "\n\n".join(snippets)[:4500]
            return (
                f"[Total: {total_len} chars | Keyword '{keyword}' matched {len(all_matches)} times (sample offsets: {offsets_preview})]\n"
                f"{combined}"
            )
        else:
            if start_char > 0 and start_char < total_len:
                chunk = text[start_char : start_char + 6500]
                return (
                    f"[Keyword '{keyword}' not found, showing text at requested start_char {start_char}..{start_char + len(chunk)}]\n{chunk}"
                )
            return (
                f"[Keyword '{keyword}' NOT FOUND anywhere in {total_len} chars on '{cache_url}'. "
                f"If you want to search the website for '{keyword}', call search_on_website(url='{cache_url}', query='{keyword}').]"
            )

    raw_end = min(total_len, start_char + 6500)
    if raw_end < total_len:
        nl_pos = text.rfind("\n", start_char + 5000, min(total_len, start_char + 6900))
        end_char = (nl_pos + 1) if nl_pos > start_char + 4600 else raw_end
    else:
        end_char = total_len

    if memory is not None:
        memory.record_chunk_read(cache_url, end_char, total_len)

    chunk = text[start_char:end_char]
    low_u = cache_url.lower()
    more_hint = (
        f" — Call start_char={end_char} on THIS URL to read unread chars {end_char}..{total_len}."
        if end_char < total_len
        else " — END OF PAGE REACHED."
    )

    dom_sample = DOM_SAMPLE_CACHE.get(cache_url, "")
    dom_note = f"\n[DOM Structure Sample (use with extract_with_selectors)]:\n{dom_sample}\n" if (start_char == 0 and dom_sample) else ""
    reviews_section = ""
    workflow_tip = ""

    if "/product-reviews/" in low_u or "/reviews/" in low_u:
        workflow_tip = (
            "\n[ACTION TIP: Reviews page loaded. Save extracted rows using save_to_csv(..., append=True) before opening the next item.]"
        )
    elif any(p in low_u for p in ("/p/itm", "/dp/", "/gp/product/")) and start_char == 0 and total_len > 10000:
        m_rev = re.search(
            r"(?:Top reviews from|Customer reviews|Ratings & Reviews|Customer Reviews|Reviewed in [^\n]+ on|\d+ customer reviews|\b\d+\s+(?:global ratings|ratings and reviews))",
            text,
            re.IGNORECASE,
        )
        if m_rev and m_rev.start() > end_char:
            rev_pos = max(0, m_rev.start() - 30)
            rev_chunk = text[rev_pos : rev_pos + 4500]
            reviews_section = (
                f"\n\n[JUMPED DIRECTLY TO CUSTOMER REVIEWS (char {rev_pos}/{total_len})]:\n{rev_chunk}\n"
            )

    return f"[Total: {total_len} chars | Showing {start_char} to {end_char}{more_hint}]{dom_note}{workflow_tip}\n{chunk}{reviews_section}"


# ---------------- TOOL 1B: Real Browser Infinite-Scroll Scraper ----------------
async def scroll_and_scrape(
    url: str,
    max_scrolls: int = 12,
    start_char: int = 0,
    seen_calls: set | None = None,
    memory: WorkingMemory | None = None,
) -> str:
    url = (url or "").strip()
    max_scrolls = max(1, min(int(max_scrolls or 12), 20))
    start_char = int(start_char or 0)

    if not url or not url.startswith(("http://", "https://")):
        return "Error: Provide a valid full URL starting with http:// or https://"

    cache_url = url.split("#")[0]

    if start_char > 0 and cache_url in PAGE_CACHE:
        call_key = (cache_url, start_char, "")
        if seen_calls is not None:
            if call_key in seen_calls:
                unread = memory.get_next_unread_hint() if memory else ""
                return (
                    f"DUPLICATE CALL BLOCKED: You already read start_char={start_char} on '{cache_url}'. "
                    f"{unread or 'Extract from the text you already have, open a candidate URL, or call save_to_csv.'}"
                )
            seen_calls.add(call_key)

        cleaned = PAGE_CACHE[cache_url]
        total_len = len(cleaned)
        if start_char < total_len:
            raw_end = min(total_len, start_char + 6500)
            if raw_end < total_len:
                nl_pos = cleaned.rfind("\n", start_char + 5000, min(total_len, start_char + 6900))
                end_char = (nl_pos + 1) if nl_pos > start_char + 4600 else raw_end
            else:
                end_char = total_len
            if memory is not None:
                memory.record_chunk_read(cache_url, end_char, total_len)
            chunk = cleaned[start_char:end_char]
            more_hint = (
                f" — Call start_char={end_char} to read remaining unread chars {end_char}..{total_len}."
                if end_char < total_len
                else " — END OF SCROLLED PAGE REACHED."
            )
            print(f"  [Cache Chunk] scroll_and_scrape('{url}', start_char={start_char}..{end_char}/{total_len})")
            return f"[Total: {total_len} chars | Showing {start_char} to {end_char}{more_hint}]\n{chunk}"

    print(f"  [Tool Running] scroll_and_scrape('{url}', max_scrolls={max_scrolls}, start_char={start_char})")

    _, page = await BROWSER_MANAGER.get_page(headless=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    await page.wait_for_timeout(2000)

    try:
        await page.evaluate(DISMISS_POPUPS_JS)
        await page.wait_for_timeout(400)
    except Exception:
        pass

    await handle_human_verification(page)

    initial_snap = await extract_dom_markdown(page, skip_scroll=True)
    snapshots: list[str] = [initial_snap]
    initial_chars = len(initial_snap)
    prev_merged_len = initial_chars
    no_growth_streak = 0
    scrolls_done = 0
    stop_reason = f"reached max_scrolls={max_scrolls}"

    if not _is_anti_bot_text(initial_snap) and not is_mandatory_login_wall(initial_snap):
        for s_idx in range(1, max_scrolls + 1):
            await page.evaluate(SMART_SCROLL_JS)
            try:
                await page.mouse.wheel(0, 1400)
            except Exception:
                pass
            await page.wait_for_timeout(1400)

            snap = await extract_dom_markdown(page, skip_scroll=True)
            snapshots.append(snap)
            merged = _merge_scroll_snapshots(snapshots)
            cur_merged_len = len(merged)
            delta_c = cur_merged_len - prev_merged_len

            scrolls_done = s_idx
            if delta_c > 30:
                no_growth_streak = 0
                print(f"  [Scroll {s_idx}/{max_scrolls}] collected={prev_merged_len}->{cur_merged_len} (+{delta_c})")
            else:
                no_growth_streak += 1
                print(f"  [Scroll {s_idx}/{max_scrolls}] No new content loaded ({no_growth_streak}/3)")
                if no_growth_streak >= 3:
                    stop_reason = "3 consecutive scrolls produced no new content"
                    print(f"  [Scroll Complete] Stopping: {stop_reason}.")
                    break

            prev_merged_len = cur_merged_len

    cleaned = _merge_scroll_snapshots(snapshots)
    final_chars = len(cleaned)

    if _is_anti_bot_text(cleaned):
        return (
            f"[ANTI-BOT BLOCKED on '{cache_url}': Bot/CAPTCHA check prevented scrolling ({final_chars} chars). "
            "Do NOT call login_or_signup_with_email!]"
        )

    if is_mandatory_login_wall(cleaned):
        if cache_url not in PAGE_CACHE or len(PAGE_CACHE[cache_url]) < 1500:
            PAGE_CACHE[cache_url] = cleaned
        return f"[MANDATORY LOGIN WALL DETECTED on '{cache_url}'! Call login_or_signup_with_email(url='{cache_url}') NOW!]\n{cleaned[:1500]}"

    if final_chars >= len(PAGE_CACHE.get(cache_url, "")):
        PAGE_CACHE[cache_url] = cleaned

    if seen_calls is not None:
        stale = {k for k in seen_calls if isinstance(k, tuple) and k and k[0] == cache_url}
        seen_calls.difference_update(stale)
        seen_calls.add((cache_url, start_char, ""))

    if memory is not None:
        memory.mark_visited(cache_url)

    total_len = len(cleaned)
    print(f"  [Scroll Result] Scrolls: {scrolls_done} | Initial: {initial_chars} -> Final: {final_chars} chars | Clean markdown: {total_len} chars")

    raw_end = min(total_len, start_char + 6500)
    if raw_end < total_len:
        nl_pos = cleaned.rfind("\n", start_char + 5000, min(total_len, start_char + 6900))
        end_char = (nl_pos + 1) if nl_pos > start_char + 4600 else raw_end
    else:
        end_char = total_len

    if memory is not None:
        memory.record_chunk_read(cache_url, end_char, total_len)

    chunk = cleaned[start_char:end_char]
    more_hint = (
        f" — Call scrape_website(url='{cache_url}', start_char={end_char}) to read unread chars {end_char}..{total_len}."
        if end_char < total_len
        else " — END OF SCROLLED PAGE REACHED."
    )
    return (
        f"[Infinite Scroll Complete: {scrolls_done} browser scrolls | Loaded {initial_chars} -> {final_chars} chars | Reason: {stop_reason}]\n"
        f"[Total: {total_len} chars | Showing {start_char} to {end_char}{more_hint}]\n{chunk}"
    )


# ---------------- TOOL 1C: Interactive AJAX / Button Pagination Scraper ----------------
async def ajax_paginate_and_scrape(
    url: str,
    max_pages: int = 6,
    start_char: int = 0,
    seen_calls: set | None = None,
    memory: WorkingMemory | None = None,
) -> str:
    url = (url or "").strip()
    max_pages = max(1, min(int(max_pages or 6), 25))
    start_char = int(start_char or 0)

    if not url or not url.startswith(("http://", "https://")):
        return "Error: Provide a valid full URL starting with http:// or https://"

    cache_url = url.split("#")[0]

    if start_char > 0 and cache_url in PAGE_CACHE:
        call_key = (cache_url, start_char, "")
        if seen_calls is not None:
            if call_key in seen_calls:
                unread = memory.get_next_unread_hint() if memory else ""
                return f"DUPLICATE CALL BLOCKED: Already read start_char={start_char} on '{cache_url}'. {unread}"
            seen_calls.add(call_key)

        cleaned = PAGE_CACHE[cache_url]
        total_len = len(cleaned)
        if start_char < total_len:
            raw_end = min(total_len, start_char + 6500)
            if raw_end < total_len:
                nl_pos = cleaned.rfind("\n", start_char + 5000, min(total_len, start_char + 6900))
                end_char = (nl_pos + 1) if nl_pos > start_char + 4600 else raw_end
            else:
                end_char = total_len
            if memory is not None:
                memory.record_chunk_read(cache_url, end_char, total_len)
            chunk = cleaned[start_char:end_char]
            more_hint = (
                f" — Call start_char={end_char} to read unread chars {end_char}..{total_len}."
                if end_char < total_len
                else " — END OF COLLECTED PAGES REACHED."
            )
            return f"[Total: {total_len} chars | Showing {start_char} to {end_char}{more_hint}]\n{chunk}"

    print(f"  [Tool Running] ajax_paginate_and_scrape('{url}', max_pages={max_pages}, start_char={start_char})")

    _, page = await BROWSER_MANAGER.get_page(headless=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    await page.wait_for_timeout(1800)

    try:
        await page.evaluate(DISMISS_POPUPS_JS)
        await page.wait_for_timeout(300)
    except Exception:
        pass

    await handle_human_verification(page)

    pages_collected: list[str] = []
    page1_text = await extract_dom_markdown(page, skip_scroll=True)
    pages_collected.append(f"=== PAGE 1 ===\n{page1_text}")
    prev_raw_page = page1_text
    stop_reason = f"reached max_pages={max_pages}"
    pages_done = 1
    print(f"  [AJAX Page 1/{max_pages}] Extracted ({len(page1_text)} chars)")

    if not _is_anti_bot_text(page1_text) and not is_mandatory_login_wall(page1_text):
        for p_num in range(2, max_pages + 1):
            prev_card = await page.evaluate("""
            () => {
                const card = document.querySelector('div.thumbnail, article, .product-item, .card, tbody tr');
                return card ? card.innerText.trim() : (document.body.innerText || '').trim().slice(0, 500);
            }
            """)

            next_selectors = [
                "button.btn.next:not([disabled])",
                "button.next:not([disabled])",
                "a.next:not(.disabled)",
                "a[rel='next']",
                ".pagination button:has-text('Next'):not([disabled])",
                ".pagination a:has-text('Next')",
                ".pagination li.next:not(.disabled) a",
                ".pagination li.next:not(.disabled) button",
                "button[aria-label*='next' i]:not([disabled])",
                "a[aria-label*='next' i]",
                f".pagination button:text-is('{p_num}'):not([disabled])",
                f".pagination a:text-is('{p_num}')",
                f"button.page-link:text-is('{p_num}'):not([disabled])",
                f"a.page-link:text-is('{p_num}')",
                "button:has-text('Load more'):not([disabled])",
                "button:has-text('Show more'):not([disabled])",
                "button:has-text('Next'):not([disabled])",
                "a:has-text('Next')",
            ]

            clicked_btn = None
            for sel in next_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=350):
                        cls_attr = (await loc.get_attribute("class") or "").lower()
                        dis_attr = await loc.get_attribute("disabled")
                        if "disabled" in cls_attr or dis_attr is not None:
                            continue
                        await loc.scroll_into_view_if_needed()
                        await loc.click()
                        clicked_btn = sel
                        break
                except Exception:
                    pass

            if not clicked_btn:
                stop_reason = f"no more enabled pagination buttons after page {pages_done}"
                print(f"  [AJAX Stop] {stop_reason}")
                break

            try:
                await page.wait_for_function(
                    """(prev) => {
                        const card = document.querySelector('div.thumbnail, article, .product-item, .card, tbody tr');
                        const cur = card ? card.innerText.trim() : (document.body.innerText || '').trim().slice(0, 500);
                        return cur && cur !== prev;
                    }""",
                    arg=prev_card,
                    timeout=8000,
                )
                await page.wait_for_timeout(350)
            except Exception:
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1200)

            cur_text = await extract_dom_markdown(page, skip_scroll=True)
            if cur_text == prev_raw_page:
                stop_reason = f"pagination click '{clicked_btn}' produced no DOM change"
                print(f"  [AJAX Stop] {stop_reason}")
                break

            pages_done = p_num
            if len(cur_text) > len(prev_raw_page) + 50 and cur_text[:200] == prev_raw_page[:200]:
                pages_collected = [f"=== LOADED UP TO PAGE {p_num} ===\n{cur_text}"]
            else:
                pages_collected.append(f"=== PAGE {p_num} ===\n{cur_text}")
            prev_raw_page = cur_text
            print(f"  [AJAX Page {p_num}/{max_pages}] Clicked '{clicked_btn}' -> DOM updated ({len(cur_text)} chars)")

    cleaned = "\n\n".join(pages_collected)

    if _is_anti_bot_text(page1_text):
        return (
            f"[ANTI-BOT BLOCKED on '{cache_url}': Bot/CAPTCHA check prevented pagination ({len(page1_text)} chars). "
            "Do NOT call login_or_signup_with_email!]"
        )

    if is_mandatory_login_wall(page1_text):
        if cache_url not in PAGE_CACHE or len(PAGE_CACHE[cache_url]) < 1500:
            PAGE_CACHE[cache_url] = cleaned
        return f"[MANDATORY LOGIN WALL DETECTED on '{cache_url}'! Call login_or_signup_with_email(url='{cache_url}') NOW!]\n{cleaned[:1500]}"

    if len(cleaned) >= len(PAGE_CACHE.get(cache_url, "")):
        PAGE_CACHE[cache_url] = cleaned

    if seen_calls is not None:
        stale = {k for k in seen_calls if isinstance(k, tuple) and k and k[0] == cache_url}
        seen_calls.difference_update(stale)
        seen_calls.add((cache_url, start_char, ""))

    if memory is not None:
        memory.mark_visited(cache_url)

    total_len = len(cleaned)
    print(f"  [AJAX Result] Pages collected: {pages_done} | Clean length: {total_len} chars | Reason: {stop_reason}")

    raw_end = min(total_len, start_char + 6500)
    if raw_end < total_len:
        nl_pos = cleaned.rfind("\n", start_char + 5000, min(total_len, start_char + 6900))
        end_char = (nl_pos + 1) if nl_pos > start_char + 4600 else raw_end
    else:
        end_char = total_len

    if memory is not None:
        memory.record_chunk_read(cache_url, end_char, total_len)

    chunk = cleaned[start_char:end_char]
    more_hint = (
        f" — Call scrape_website(url='{cache_url}', start_char={end_char}) to read unread chars {end_char}..{total_len}."
        if end_char < total_len
        else " — END OF COLLECTED PAGES REACHED."
    )
    return (
        f"[AJAX Pagination Complete: {pages_done} pages collected | Reason: {stop_reason}]\n"
        f"[Total: {total_len} chars | Showing {start_char} to {end_char}{more_hint}]\n{chunk}"
    )


# ---------------- TOOL 1D: Fast Deterministic DOM Selector Extractor ----------------
async def extract_with_selectors(
    url: str,
    item_selector: str,
    field_selectors: dict[str, str],
    filename: str,
    max_items: int = 50,
    mode: str = "single",
    unique_fields: list[str] | None = None,
    append: bool = False,
    login_used: bool = False,
    login_skipped: bool = False,
    seen_calls: set | None = None,
    memory: WorkingMemory | None = None,
) -> str:
    url = (url or "").strip()
    item_selector = (item_selector or "").strip()
    mode = (mode or "single").strip().lower()
    max_items = max(1, min(int(max_items or 50), 200))

    if not url or not url.startswith(("http://", "https://")):
        return "Error: Provide a valid full URL starting with http:// or https://"
    if not item_selector or not isinstance(field_selectors, dict) or not field_selectors:
        return "Error: item_selector and non-empty field_selectors object are required."

    cache_url = url.split("#")[0]

    sel_key = ("SELECTOR", cache_url, item_selector, tuple(sorted((str(k), str(v)) for k, v in field_selectors.items())), mode)
    if seen_calls is not None:
        if sel_key in seen_calls:
            print(f"  [Blocked Duplicate Selector] '{item_selector}' already tried on '{cache_url[:50]}...'")
            return (
                f"DUPLICATE SELECTOR BLOCKED: You already tried item_selector='{item_selector}' (mode='{mode}') on this URL. "
                "STOP repeating extract_with_selectors with this selector! "
                "Read the cached page text with scrape_website(url=..., start_char=...) or open individual URLs from [AGENT WORKING MEMORY]."
            )
        seen_calls.add(sel_key)

    print(f"  [Tool Running] extract_with_selectors('{url}', item='{item_selector}', mode='{mode}', max_items={max_items})")

    js_extract = """
    ({ rootSelector, fieldsMap }) => {
        let rawCards;
        try {
            rawCards = Array.from(document.querySelectorAll(rootSelector));
        } catch (err) {
            return { __error: `Invalid item_selector '${rootSelector}': ${err.message}` };
        }
        const cards = rawCards.filter(c => !rawCards.some(other => other !== c && c.contains(other)));
        const rows = cards.map(card => {
            const obj = {};
            for (const [col, sel] of Object.entries(fieldsMap)) {
                let attrMode = null;
                let cssSel = (sel || '').trim();
                const attrMatch = cssSel.match(/^(.*)::attr\\(([^)]+)\\)$/);
                if (attrMatch) {
                    cssSel = attrMatch[1].trim();
                    attrMode = attrMatch[2].trim();
                }
                let els;
                try {
                    els = cssSel ? Array.from(card.querySelectorAll(cssSel)) : [card];
                } catch (err) {
                    return { __error: `Invalid field_selector '${cssSel}' for '${col}': ${err.message}` };
                }
                if (!els.length) {
                    obj[col] = '';
                    continue;
                }
                const colLow = col.toLowerCase();
                const vals = els.map(el => {
                    if (attrMode) return (el.getAttribute(attrMode) || el[attrMode] || '').trim();
                    if (colLow.includes('url') || colLow.includes('link')) {
                        const aEl = el.tagName === 'A' ? el : (el.closest('a') || el.querySelector('a'));
                        if (aEl && aEl.href) return aEl.href.trim();
                    }
                    let t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    const fullTitle = ((el.getAttribute && el.getAttribute('title')) || '').trim().replace(/\\s+/g, ' ');
                    if (fullTitle.length > t.length && (t.endsWith('...') || t.endsWith('…'))) {
                        t = fullTitle;
                    }
                    return t;
                }).filter(Boolean);
                obj[col] = vals.join(', ');
            }
            return obj;
        });
        return { rows };
    }
    """

    _, page = await BROWSER_MANAGER.get_page(headless=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    await page.wait_for_timeout(1800)

    try:
        await page.evaluate(DISMISS_POPUPS_JS)
        await page.wait_for_timeout(300)
    except Exception:
        pass

    await handle_human_verification(page)

    body_check = await page.evaluate("() => document.body.innerText || ''")
    if _is_anti_bot_text(body_check):
        if cache_url in PAGE_CACHE and len(PAGE_CACHE[cache_url]) >= 1500:
            return (
                f"SELECTOR FAILED: Anti-bot check blocked live selector navigation on '{url}', "
                f"but {len(PAGE_CACHE[cache_url])} chars are ALREADY cached! "
                f"Use scrape_website(url='{cache_url}', start_char=0) and save_to_csv directly."
            )
        return (
            f"[ANTI-BOT BLOCKED on '{cache_url}': Bot/CAPTCHA check blocked selector extraction ({len(body_check)} chars). "
            "Do NOT call login_or_signup_with_email!]"
        )

    if is_mandatory_login_wall(body_check):
        if cache_url not in PAGE_CACHE or len(PAGE_CACHE[cache_url]) < 1500:
            PAGE_CACHE[cache_url] = body_check
        return f"[MANDATORY LOGIN WALL DETECTED on '{cache_url}'! Call login_or_signup_with_email(url='{cache_url}') NOW!]"

    keys = list(field_selectors.keys())
    collected: list[dict] = []
    seen_sigs: set[tuple] = set()
    grounding_corpus: list[str] = [body_check]

    def _ingest(batch: list[dict]) -> int:
        added = 0
        for row in batch:
            if len(collected) >= max_items:
                break
            if not any(str(v).strip() for v in row.values()):
                continue
            sig = _row_signature(row, keys, unique_fields)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            collected.append(row)
            added += 1
        return added

    res = await page.evaluate(js_extract, {"rootSelector": item_selector, "fieldsMap": field_selectors})
    if isinstance(res, dict) and res.get("__error"):
        return (
            f"SELECTOR FAILED: {res['__error']}. Use standard CSS selectors only (never :has-text). "
            "Check [DOM Structure Sample] or fall back to scrape_website + save_to_csv."
        )
    initial_batch = res.get("rows", []) if isinstance(res, dict) else []
    for r in initial_batch:
        if isinstance(r, dict) and r.get("__error"):
            return (
                f"SELECTOR FAILED: {r['__error']}. Use standard CSS selectors only (never :has-text). "
                "Check [DOM Structure Sample] or fall back to scrape_website + save_to_csv."
            )

    _ingest(initial_batch)
    print(f"  [Selector Initial] Extracted {len(collected)}/{max_items} unique items")

    if mode == "scroll" and collected:
        stalls = 0
        for s_idx in range(1, 25):
            if len(collected) >= max_items:
                break
            await page.evaluate(SMART_SCROLL_JS)
            try:
                await page.mouse.wheel(0, 1400)
            except Exception:
                pass
            await page.wait_for_timeout(1300)

            grounding_corpus.append(await page.evaluate("() => document.body.innerText || ''"))
            res = await page.evaluate(js_extract, {"rootSelector": item_selector, "fieldsMap": field_selectors})
            batch = res.get("rows", []) if isinstance(res, dict) else []
            added = _ingest(batch)
            print(f"  [Selector Scroll {s_idx}] +{added} new | Total: {len(collected)}/{max_items}")
            if added == 0:
                stalls += 1
                if stalls >= 3:
                    print("  [Selector Scroll Stop] 3 consecutive scrolls produced no new items.")
                    break
            else:
                stalls = 0

    elif mode in ("ajax", "url_pagination") and collected:
        for p_num in range(2, 26):
            if len(collected) >= max_items:
                break

            navigated = False
            if mode == "url_pagination":
                next_href = await page.evaluate("""
                (pNum) => {
                    const candidates = [
                        "a[rel='next']",
                        ".pagination li.next:not(.disabled) a",
                        ".pagination a.next:not(.disabled)",
                        "a.next:not(.disabled)",
                        `a[href*='page=${pNum}']`,
                        `a[href*='p=${pNum}']`,
                        `a[href*='/page/${pNum}']`
                    ];
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el && el.href) return el.href;
                    }
                    for (const a of document.querySelectorAll('a')) {
                        const t = (a.innerText || '').trim().toLowerCase();
                        if ((t === 'next' || t === 'next »' || t === '›' || t === String(pNum)) && a.href && !a.href.startsWith('javascript:')) {
                            return a.href;
                        }
                    }
                    return '';
                }
                """, p_num)
                if next_href:
                    next_abs = urljoin(page.url, next_href)
                    if next_abs != page.url:
                        await page.goto(next_abs, wait_until="domcontentloaded", timeout=35000)
                        await page.wait_for_timeout(1200)
                        navigated = True

            if not navigated:
                prev_card = await page.evaluate("""
                (sel) => {
                    const el = document.querySelector(sel);
                    return el ? el.innerText.trim() : (document.body.innerText || '').slice(0, 400);
                }
                """, item_selector)

                next_selectors = [
                    "button.btn.next:not([disabled])",
                    "button.next:not([disabled])",
                    "a.next:not(.disabled)",
                    "a[rel='next']",
                    ".pagination button:has-text('Next'):not([disabled])",
                    ".pagination a:has-text('Next')",
                    ".pagination li.next:not(.disabled) a",
                    ".pagination li.next:not(.disabled) button",
                    "button[aria-label*='next' i]:not([disabled])",
                    "a[aria-label*='next' i]",
                    f".pagination button:text-is('{p_num}'):not([disabled])",
                    f".pagination a:text-is('{p_num}')",
                    f"button.page-link:text-is('{p_num}'):not([disabled])",
                    f"a.page-link:text-is('{p_num}')",
                    "button:has-text('Load more'):not([disabled])",
                    "button:has-text('Show more'):not([disabled])",
                    "button:has-text('Next'):not([disabled])",
                    "a:has-text('Next')",
                ]

                clicked = False
                for nsel in next_selectors:
                    try:
                        loc = page.locator(nsel).first
                        if await loc.is_visible(timeout=350):
                            cls_attr = (await loc.get_attribute("class") or "").lower()
                            dis_attr = await loc.get_attribute("disabled")
                            if "disabled" in cls_attr or dis_attr is not None:
                                continue
                            await loc.scroll_into_view_if_needed()
                            await loc.click()
                            clicked = True
                            break
                    except Exception:
                        pass

                if not clicked:
                    print(f"  [Selector Paginate Stop] No more Next buttons after page {p_num - 1}.")
                    break

                try:
                    await page.wait_for_function(
                        """({ sel, prev }) => {
                            const el = document.querySelector(sel);
                            const cur = el ? el.innerText.trim() : (document.body.innerText || '').slice(0, 400);
                            return cur && cur !== prev;
                        }""",
                        arg={"sel": item_selector, "prev": prev_card},
                        timeout=8000,
                    )
                    await page.wait_for_timeout(300)
                except Exception:
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(1200)

            grounding_corpus.append(await page.evaluate("() => document.body.innerText || ''"))
            res = await page.evaluate(js_extract, {"rootSelector": item_selector, "fieldsMap": field_selectors})
            batch = res.get("rows", []) if isinstance(res, dict) else []
            added = _ingest(batch)
            print(f"  [Selector Page {p_num}] +{added} new | Total: {len(collected)}/{max_items}")
            if added == 0:
                break

    if not collected:
        return (
            f"SELECTOR FAILED: 0 items matched item_selector='{item_selector}' on '{url}'. "
            "Do NOT guess another selector unless you saw it in [DOM Structure Sample]. "
            "Instead, read the already-cached text with scrape_website(url=..., start_char=...) or open individual URLs from [AGENT WORKING MEMORY]."
        )

    sel_quality_err = _validate_selector_rows(collected, field_selectors)
    if sel_quality_err:
        return sel_quality_err

    corpus_text = "\n".join(grounding_corpus)
    if len(corpus_text) >= len(PAGE_CACHE.get(cache_url, "")):
        PAGE_CACHE[cache_url] = corpus_text
    if memory is not None:
        memory.mark_visited(cache_url)

    save_msg = save_to_csv(
        filename=filename,
        items=collected,
        append=append,
        login_used=login_used,
        login_skipped=login_skipped,
        memory=memory,
        unique_fields=unique_fields,
        skip_grounding=True,
    )
    return f"extract_with_selectors extracted {len(collected)} verbatim DOM rows. {save_msg}"


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

    _, page = await BROWSER_MANAGER.get_page(headless=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    await page.wait_for_timeout(2500)

    try:
        await page.evaluate(DISMISS_POPUPS_JS)
        await page.wait_for_timeout(400)
    except Exception:
        pass

    await handle_human_verification(page)

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
                    await page.wait_for_timeout(2500)
                    print(f"  [Section Opened] Clicked '{target_section}'")
                    break
            except Exception:
                pass

    pre_text = await extract_dom_markdown(page)
    cur_url = (page.url or cache_url).split("#")[0]
    if _is_anti_bot_text(pre_text):
        return (
            f"[ANTI-BOT BLOCKED on '{cur_url}': Bot/CAPTCHA challenge blocked search ({len(pre_text)} chars). "
            "Do NOT call login_or_signup_with_email!]"
        )
    if is_mandatory_login_wall(pre_text):
        if cur_url not in PAGE_CACHE or len(PAGE_CACHE[cur_url]) < 1500:
            PAGE_CACHE[cur_url] = pre_text
        if memory is not None:
            memory.remember_login_required(True)
        print(f"  [Login Wall Detected] Mandatory login required on '{cur_url[:60]}...'")
        return f"[MANDATORY LOGIN WALL DETECTED on '{cur_url}'! Call login_or_signup_with_email(url='{cur_url}') NOW!]"

    search_selectors = [
        "input[aria-label*='Search by title' i]",
        "input[placeholder*='Search job' i]",
        "input[name='keywords']",
        "input[type='search']",
        "input[placeholder*='search' i]",
        "input[aria-label*='search' i]",
        "input[name='q']",
        "input[name='ss']",
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
        return f"Could not locate a visible search bar on '{url}'. Read '{url}' directly or open a category/search URL."

    before_url = page.url
    await search_box.click()
    await search_box.fill("")
    await search_box.type(query, delay=65)
    await page.wait_for_timeout(600)

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
            await loc_box.type(location, delay=65)
            await page.wait_for_timeout(800)
            await loc_box.press("Enter")
        else:
            await search_box.press("Enter")
    else:
        await search_box.press("Enter")

    await page.wait_for_timeout(2000)
    if page.url == before_url:
        for sub_sel in (
            "button[type='submit']",
            "input[type='submit']",
            "button[aria-label*='search' i]",
            "button:has-text('Search')",
        ):
            try:
                sbtn = page.locator(sub_sel).first
                if await sbtn.is_visible(timeout=400):
                    await sbtn.click()
                    break
            except Exception:
                pass

    await page.wait_for_timeout(3000)
    await handle_human_verification(page)

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
        await page.wait_for_timeout(1800)

    res_url = (page.url or cache_url).split("#")[0]
    res_text = await extract_dom_markdown(page)
    if len(res_text) < 350:
        await page.wait_for_timeout(2500)
        res_text = await extract_dom_markdown(page)

    if _is_anti_bot_text(res_text):
        return (
            f"[ANTI-BOT BLOCKED on '{res_url}': Anti-bot protection blocked search results ({len(res_text)} chars). "
            "Do NOT call login_or_signup_with_email! Try another URL or inform the user.]"
        )

    if is_mandatory_login_wall(res_text):
        if res_url not in PAGE_CACHE or len(PAGE_CACHE[res_url]) < 1500:
            PAGE_CACHE[res_url] = res_text
        if memory is not None:
            memory.remember_login_required(True)
        print(f"  [Login Wall Detected] Mandatory login required on '{res_url[:60]}...'")
        return f"[MANDATORY LOGIN WALL DETECTED on '{res_url}'! Call login_or_signup_with_email(url='{res_url}') NOW!]"

    raw_html = await page.content()
    structured = extract_structured_data(raw_html)
    if structured:
        res_text = f"{structured}\n\n{res_text}"
    dom_sample = extract_dom_sample(raw_html)

    if len(res_text) >= len(PAGE_CACHE.get(res_url, "")):
        PAGE_CACHE[res_url] = res_text
    if dom_sample:
        DOM_SAMPLE_CACHE[res_url] = dom_sample
    if memory is not None:
        memory.mark_visited(res_url)
        memory.extract_candidate_urls(res_text, res_url)
        memory.record_chunk_read(res_url, min(6500, len(res_text)), len(res_text))

    print(f"  [Search Complete] '{res_url}' ({len(res_text)} chars)")
    dom_note = f"\n[DOM Structure Sample (use with extract_with_selectors)]:\n{dom_sample}\n" if dom_sample else ""
    return (
        f"[Search Results URL: {res_url} | Total: {len(res_text)} chars | Showing 0 to {min(6500, len(res_text))}]{dom_note}\n"
        f"{res_text[:6500]}"
    )


# ---------------- TOOL 3: On-Demand Email Login / Sign-up ----------------
async def login_or_signup_with_email(
    url: str,
    seen_calls: set,
    memory: WorkingMemory | None = None,
    username: str = "",
    password: str = "",
) -> tuple[str, bool, bool]:
    print(f"\n  [Tool Running] login_or_signup_with_email('{url}')")

    username = (username or "").strip()
    password = (password or "").strip()

    if not username:
        prompt_msg = (
            f"\n>>> [LOGIN REQUIRED] Mandatory login is required to scrape '{url}'.\n"
            f">>> Enter Login/Signup Email or Username (press Enter for '{DEFAULT_LOGIN_EMAIL}', or type 'skip'): "
            if DEFAULT_LOGIN_EMAIL
            else f"\n>>> [LOGIN REQUIRED] Mandatory login is required to scrape '{url}'.\n"
                 ">>> Enter Login/Signup Email or Username (press Enter to skip): "
        )
        username = (await asyncio.to_thread(input, prompt_msg)).strip()
        if not username and DEFAULT_LOGIN_EMAIL:
            username = DEFAULT_LOGIN_EMAIL

    if not username or username.lower() == "skip":
        return (
            "User skipped login. Do NOT retry login; report that mandatory login was skipped.",
            True,
            False,
        )

    context, page = await BROWSER_MANAGER.get_page(headless=False)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(2500)

        try:
            body_text = (await page.inner_text("body")).lower()
            if any(e in body_text for e in ERROR_PHRASES):
                parsed_u = urlparse(url)
                home_url = f"{parsed_u.scheme}://{parsed_u.netloc}/"
                print(f"  [Notice] '{url}' is a broken/404 page; opening homepage '{home_url}'...")
                await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        if password:
            try:
                page_body_low = (await page.inner_text("body")).lower()
                if username.lower() not in page_body_low or password.lower() not in page_body_low:
                    print(f"  [Blocked Guessed Creds] '{username}' not printed on page — switching to manual login.")
                    if username.lower() in {"admin", "user", "test", "demo", "email"}:
                        username = ""
                    password = ""
            except Exception:
                password = ""

        user_selectors = [
            "input[type='email']",
            "input[name*='email' i]",
            "input[id*='email' i]",
            "input[placeholder*='email' i]",
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[placeholder*='user' i]",
            "input[data-test='username']",
        ]

        async def _find_first_visible(selectors):
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=400):
                        return loc
                except Exception:
                    pass
            return None

        user_input = await _find_first_visible(user_selectors)

        if not user_input:
            for sel in (
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
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=500):
                        await loc.click()
                        await page.wait_for_timeout(1200)
                        print(f"  [Auto-Clicked] '{sel}'")
                        user_input = await _find_first_visible(user_selectors)
                        break
                except Exception:
                    pass

        if user_input and username:
            await user_input.fill(username)
            print(f"  [Auto-Filled] Username/Email '{username}' entered!")
        else:
            print("  [Notice] Enter your Username/Email manually in the browser window.")

        auto_submitted = False
        if password:
            pw_input = await _find_first_visible(["input[type='password']", "input[name*='pass' i]"])
            if pw_input:
                await pw_input.fill(password)
                submit_btn = await _find_first_visible([
                    "input[type='submit']",
                    "button[type='submit']",
                    "#login-button",
                    "button:has-text('Login')",
                    "button:has-text('Sign in')",
                ])
                if submit_btn:
                    await submit_btn.click()
                    await page.wait_for_timeout(2500)
                    auto_submitted = True
                    print("  [Auto-Submitted] Demo credentials submitted.")

        if not auto_submitted:
            print("\n>>> BROWSER WINDOW IS OPEN <<<")
            print(">>> Complete Password / OTP / CAPTCHA and click Login inside the browser.")
            await asyncio.to_thread(input, ">>> Press ENTER here ONLY AFTER the logged-in page loads: ")

        active_page = context.pages[-1] if context.pages else page
        auth_verified, final_url, unlocked_text, summary = await verify_authenticated_session(
            active_page, context, original_url=url
        )
        if auto_submitted and not auth_verified:
            print("\n>>> Auto-submit did not unlock the page. Complete login manually in Chrome <<<")
            await asyncio.to_thread(input, ">>> Press ENTER here ONLY AFTER the logged-in page loads: ")
            active_page = context.pages[-1] if context.pages else page
            auth_verified, final_url, unlocked_text, summary = await verify_authenticated_session(
                active_page, context, original_url=url
            )

        print(f"  [Auth Check] verified={auth_verified} | {summary}")

        PAGE_CACHE.clear()
        seen_calls.clear()

        if not auth_verified:
            return (
                f"[AUTHENTICATION FAILED]: Login could not be verified ({summary}). "
                "Do NOT silently scrape another public page. Stop and report authentication failure.",
                False,
                False,
            )

        try:
            BROWSER_MANAGER.set_auth_cookies(await context.cookies())
        except Exception:
            pass

        PAGE_CACHE[final_url] = unlocked_text
        if memory is not None:
            memory.mark_visited(final_url)
            memory.extract_candidate_urls(unlocked_text, final_url)
            memory.record_chunk_read(final_url, min(6500, len(unlocked_text)), len(unlocked_text))

        return (
            f"[AUTHENTICATION VERIFIED]: Login succeeded ({summary}). "
            f"Authenticated page '{final_url}' ({len(unlocked_text)} chars) is ALREADY cached. "
            f"Call scrape_website(url='{final_url}') NOW to extract the data.",
            False,
            True,
        )
    finally:
        await BROWSER_MANAGER.close()


# ---------------- TOOL 4: Grounded, Validated & Deduplicated CSV Saver ----------------
def _is_placeholder(val) -> bool:
    if val is None:
        return True
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


def _validate_semantic_mapping(items: list[dict]) -> str | None:
    for idx, item in enumerate(items, 1):
        for col, val in item.items():
            if val is None or _is_placeholder(val):
                continue
            col_low = str(col).strip().lower()
            sval = str(val).strip()
            sval_low = sval.lower()
            if any(w in col_low for w in _EXEMPT_WORDS):
                continue

            if any(h in col_low for h in _TEXT_COL_HINTS) and not any(h in col_low for h in _NUMERIC_COL_HINTS):
                if re.fullmatch(r"[\d,.\s]+", sval):
                    return (
                        f"REJECTED (Semantic Mapping Error in row {idx}): Column '{col}' expects a text/name value, "
                        f"but received numeric '{sval}'. Check your key-to-value mapping and retry."
                    )

            if any(h in col_low for h in _NUMERIC_COL_HINTS):
                if any(st in sval_low for st in _ALLOWED_NUMERIC_STATUS):
                    continue
                if re.search(r"[A-Za-z]", sval) and not re.search(r"\d", sval):
                    return (
                        f"REJECTED (Semantic Mapping Error in row {idx}): Column '{col}' expects a numeric value, "
                        f"but received text '{sval}'. Check your key-to-value mapping and retry."
                    )
    return None


def _check_grounding(items: list[dict]) -> str | None:
    if not PAGE_CACHE:
        return None
    raw_corpus = " ".join(PAGE_CACHE.values())
    if len(raw_corpus) < 100:
        return None
    norm_corpus = re.sub(r"[^a-z0-9]+", " ", raw_corpus.lower())

    for idx, item in enumerate(items, 1):
        candidates = []
        for col, val in item.items():
            if val is None or _is_placeholder(val):
                continue
            col_low = str(col).lower()
            if "url" in col_low or "link" in col_low or "tag" in col_low:
                continue
            sval = re.sub(r"[^a-z0-9]+", " ", str(val).lower()).strip()
            if len(sval) >= 18 and re.search(r"[a-z]", sval):
                candidates.append((col, str(val).strip(), sval))

        if not candidates:
            continue

        candidates.sort(key=lambda x: len(x[2]), reverse=True)
        col, orig_val, norm_val = candidates[0]
        probe = norm_val[:26].strip()
        if len(probe) >= 16 and probe not in norm_corpus:
            return (
                f"REJECTED (HALLUCINATION DETECTED in row {idx}): {col}='{orig_val[:60]}...' "
                "was NOT found in any scraped page content! NEVER invent records from memory. "
                "Read the next unread start_char chunk or use extract_with_selectors."
            )
    return None


def _row_val(row: dict, k: str) -> str:
    if k in row:
        return str(row[k])
    k_low = k.lower()
    for rk, rv in row.items():
        if rk.lower() == k_low:
            return str(rv)
    return ""


def _get_item_val(it: dict, k: str):
    if k in it:
        val = it[k]
        return "" if val is None else val
    k_low = k.lower()
    for rk, rv in it.items():
        if rk.lower() == k_low:
            return "" if rv is None else rv
    return ""


def _row_signature(row: dict, keys: list[str], unique_fields: list[str] | None = None) -> tuple:
    keys_low_map = {k.lower(): k for k in keys}
    matched_keys = []
    for uf in (unique_fields or []):
        if uf in keys:
            matched_keys.append(uf)
        elif uf.lower() in keys_low_map:
            matched_keys.append(keys_low_map[uf.lower()])

    sig_keys = matched_keys or list(keys)

    if len(sig_keys) == 1 and any(n in sig_keys[0].lower() for n in ("name", "title", "product", "model", "book")):
        for extra in ("description", "desc", "price", "year", "author", "url", "link", "reviewer", "review"):
            for k in keys:
                if extra in k.lower() and k not in sig_keys:
                    sig_keys.append(k)
                    break

    return tuple(re.sub(r"\s+", " ", _row_val(row, k)).strip().lower() for k in sig_keys)


def save_to_csv(
    filename: str,
    items: list[dict],
    append: bool = True,
    login_used: bool = False,
    login_skipped: bool = False,
    memory: WorkingMemory | None = None,
    unique_fields: list[str] | None = None,
    skip_grounding: bool = False,
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

    sem_error = _validate_semantic_mapping(valid_items)
    if sem_error:
        print(f"  [Validation Blocked] {sem_error}")
        return sem_error

    if not skip_grounding:
        grounding_error = _check_grounding(valid_items)
        if grounding_error:
            print(f"  [Grounding Blocked] {grounding_error}")
            return grounding_error

    full_path = resolve_csv_path(filename)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    file_exists = os.path.exists(full_path) and os.path.getsize(full_path) > 0

    existing_sigs: set[tuple] = set()
    keys: list[str] = []

    if append and file_exists:
        with open(full_path, mode="r", newline="", encoding="utf-8-sig") as rf:
            reader = list(csv.reader(rf))
            if reader:
                keys = reader[0]
                for existing_row in reader[1:]:
                    row_dict = {keys[i]: (existing_row[i] if i < len(existing_row) else "") for i in range(len(keys))}
                    existing_sigs.add(_row_signature(row_dict, keys, unique_fields))

    if append and file_exists and keys:
        key_map = {k.lower(): k for k in keys}
        normalized_items = []
        for it in valid_items:
            norm_it = {}
            for k, v in it.items():
                norm_it[key_map.get(k.lower(), k)] = v
            normalized_items.append(norm_it)
        valid_items = normalized_items

        incoming_keys = set()
        for it in valid_items:
            incoming_keys.update(it.keys())
        overlap = [k for k in keys if k in incoming_keys]
        if len(overlap) < max(1, len(keys) // 2):
            return (
                f"REJECTED (Header Mismatch): Existing CSV columns are {keys}, "
                f"but you passed {sorted(incoming_keys)}. Reuse exact column names {keys}."
            )
        mode = "a"
        write_header = False
    else:
        if memory is not None and memory.saved_headers:
            key_map = {k.lower(): k for k in memory.saved_headers}
            normalized_items = []
            for it in valid_items:
                norm_it = {}
                for k, v in it.items():
                    norm_it[key_map.get(k.lower(), k)] = v
                normalized_items.append(norm_it)
            valid_items = normalized_items

        keys = []
        for item in valid_items:
            for k in item.keys():
                if k not in keys:
                    keys.append(k)
        mode = "w"
        write_header = True
        existing_sigs.clear()

    deduped_items: list[dict] = []
    dup_count = 0
    for item in valid_items:
        sig = _row_signature(item, keys, unique_fields)
        if sig in existing_sigs:
            dup_count += 1
            continue
        existing_sigs.add(sig)
        clean_row = {k: _get_item_val(item, k) for k in keys}
        deduped_items.append(clean_row)

    if not deduped_items:
        total_now = len(existing_sigs)
        unread = memory.get_next_unread_hint() if memory else ""
        return (
            f"[ZERO-PROGRESS ERROR]: 0 new unique rows saved (all {len(valid_items)} submitted rows were duplicates already in CSV). "
            f"Total unique rows in CSV: {total_now}. STOP calling save_to_csv with already-seen items! "
            f"{unread or 'Move to the next unread page/chunk or open unvisited URLs.'}"
        )

    with open(full_path, mode=mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(deduped_items)

    total_now = len(existing_sigs)
    if memory is not None:
        memory.record_saved_rows(deduped_items, headers=keys, total_count=total_now)

    print(f"  [CSV Saved] +{len(deduped_items)} unique rows (skipped {dup_count} dups) | Total: {total_now} -> {full_path}")
    for row in deduped_items:
        print(f"    -> {row}")
    unread = memory.get_next_unread_hint() if memory else ""
    next_msg = f" Next unread content: {unread}" if unread else ""
    return (
        f"Saved {len(deduped_items)} new unique rows (skipped {dup_count} duplicates). "
        f"Total unique rows in CSV: {total_now}. Columns: {keys}.{next_msg}"
    )