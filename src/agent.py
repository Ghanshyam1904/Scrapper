import asyncio
import os
import re

from src.config import (
    LLM_PROVIDER,
    SYSTEM_PROMPT_FILE,
    PAGE_CACHE,
    DOM_SAMPLE_CACHE,
    load_system_prompt,
    resolve_csv_path,
)
from src.memory import WorkingMemory
from src.browser import BROWSER_MANAGER, is_404_page, is_mandatory_login_wall
from src.llm import build_tools, trim_messages, call_llm, get_active_model
from src.tools import (
    scrape_website,
    scroll_and_scrape,
    ajax_paginate_and_scrape,
    extract_with_selectors,
    search_on_website,
    login_or_signup_with_email,
    save_to_csv,
)


def _parse_append_flag(raw_val) -> bool:
    if raw_val is None:
        return True
    if isinstance(raw_val, str):
        return raw_val.strip().lower() not in ("false", "0", "no", "")
    return bool(raw_val)


async def run_agent(url: str, user_goal: str, csv_filename: str, max_steps: int = 30):
    url = url.strip().rstrip("\\")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url

    count_match = re.search(
        r"\b(?:first|top|at least)\s+(\d+)\b|\b(\d+)\s+(?:countries|records|items|products|listings|mobiles?|phones?|laptops?|jobs?|books?|quotes?|articles?|reviews?|rows?|teams?|hotels?|restaurants?|places?|companies|posts?|threads?|comments?|stories|headlines?|movies?|songs?|universities|players?|matches?|tools?|software|results?|entries)\b",
        user_goal,
        re.IGNORECASE,
    )
    is_nested_request = bool(re.search(r"\b(?:each|per\s+\w+|every)\b", user_goal, re.IGNORECASE))
    requested_count = (
        int(count_match.group(1) or count_match.group(2))
        if (count_match and not is_nested_request)
        else None
    )

    target_cache_url = url.split("#")[0].strip().rstrip("/\\")
    strict_scroll_only = "/scroll" in url.lower()

    csv_filename = resolve_csv_path(csv_filename)
    PAGE_CACHE.clear()
    DOM_SAMPLE_CACHE.clear()
    seen_calls: set[tuple] = set()
    memory = WorkingMemory(url, requested_count=requested_count)
    system_prompt = load_system_prompt(SYSTEM_PROMPT_FILE)
    tools = build_tools()

    print(f"\n[Agent Started] Provider: {LLM_PROVIDER.upper()} | Model: {get_active_model()} | Output: {csv_filename}")

    reviews_instruction = ""
    if any(w in user_goal.lower() for w in ("review", "reviews", "reviewer")):
        reviews_instruction = (
            "\nWhen extracting items with customer reviews: on each item page, extract the item details plus its reviews "
            "and call save_to_csv(append=True) IMMEDIATELY before opening the next item URL."
        )

    full_prompt = (
        f"Target URL: {url}\n"
        f"User Requirement (extract ONLY what is asked): {user_goal}\n"
        f"Save extracted records into CSV file: '{csv_filename}'.\n"
        "First call scrape_website on the Target URL. "
        "If [DOM Structure Sample] shows clear repeating cards/rows, you may use extract_with_selectors. "
        "On complex/obfuscated sites or when opening individual detail URLs, "
        "DO NOT keep guessing CSS selectors—read the text/tables/JSON-LD via scrape_website(start_char=...) or open exact URLs from [AGENT WORKING MEMORY] and call save_to_csv directly. "
        "NEVER invent or guess item URLs—only open URLs seen verbatim on scraped pages or in [AGENT WORKING MEMORY]. "
        "If the page lacks the requested topic and has a search bar, use search_on_website. "
        "If the page uses infinite scrolling, call scroll_and_scrape or extract_with_selectors(mode='scroll') without switching URLs. "
        "Use the EXACT field names requested by the user as CSV headers."
        f"{reviews_instruction}"
    )

    messages: list = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": full_prompt},
    ]

    saved_csv = False
    consecutive_nudges = 0
    dup_redirect_count = 0
    consecutive_dups = 0
    consecutive_zero_saves = 0
    selector_fail_streak = 0
    anti_bot_hits = 0
    login_used = False
    login_skipped = False
    auth_verified = False
    auth_failed = False
    auth_required = False
    step = 0

    try:
        for step in range(1, max_steps + 1):
            print(f"\n--- Agent Step {step} ({get_active_model()}) ---")
            safe_messages = trim_messages(messages, memory=memory)
            history_msg, text_content, tool_calls = await call_llm(safe_messages, tools, step)
            messages.append(history_msg)

            if not tool_calls:
                incomplete = bool(
                    requested_count
                    and memory.total_saved_count < requested_count
                    and not login_skipped
                )
                if anti_bot_hits >= 2 and not saved_csv and not memory.visited_urls:
                    print("\nAgent Final Response:\n", text_content)
                    break

                if (not saved_csv or incomplete) and not login_skipped and consecutive_nudges < 4:
                    consecutive_nudges += 1
                    unread_hint = memory.get_next_unread_hint()

                    if not saved_csv and memory.visited_urls:
                        print(f"  [Guardrail] '{csv_filename}' has not been created yet; forcing save/continue ({consecutive_nudges}/4)...")
                        nudge_msg = (
                            f"MANDATORY ACTION: '{csv_filename}' still has 0 rows! "
                            f"Do NOT respond with conversational text. Either call save_to_csv(filename='{csv_filename}', items=[...], append=True) "
                            f"with the exact fields requested in '{user_goal}', or if the current page ({memory.visited_urls[-1]}) doesn't have the listings yet, "
                            f"call search_on_website(url='{memory.visited_urls[-1]}', query='...') or open a candidate URL from [AGENT WORKING MEMORY]."
                        )
                    elif not saved_csv:
                        if anti_bot_hits > 0:
                            print("  [Stopped] Target URL blocked by anti-bot protection before any page loaded.")
                            break
                        print(f"  [Guardrail] No data saved yet; prompting agent to scrape '{url}' ({consecutive_nudges}/4)...")
                        nudge_msg = (
                            f"You have not extracted or saved any data yet. Call scrape_website(url='{url}') "
                            "or search_on_website(...) to gather the requested records and save them via save_to_csv."
                        )
                    else:
                        if memory.pending_urls and not unread_hint:
                            next_url = memory.pending_urls[0]
                            print(f"  [Guardrail] Incomplete ({memory.total_saved_count}/{requested_count} saved); directing to '{next_url[:50]}...' ({consecutive_nudges}/4)...")
                            nudge_msg = (
                                f"INCOMPLETE: Saved {memory.total_saved_count}/{requested_count} records so far. "
                                f"Open '{next_url}' via scrape_website(url='{next_url}') or continue paginating/scrolling, "
                                f"then save the remaining records using headers {memory.saved_headers}."
                            )
                        else:
                            print(f"  [Guardrail] Incomplete ({memory.total_saved_count}/{requested_count} saved); prompting continuation ({consecutive_nudges}/4)...")
                            nudge_msg = (
                                f"INCOMPLETE: User requested {requested_count} unique records, "
                                f"but only {memory.total_saved_count} are saved in CSV so far! "
                                f"{unread_hint or f'Read unvisited URLs from [AGENT WORKING MEMORY] or scroll/paginate on {url}.'} "
                                f"Save the remaining {requested_count - memory.total_saved_count} records using exact headers {memory.saved_headers}."
                            )
                    messages.append({"role": "user", "content": nudge_msg})
                    continue

                print("\nAgent Final Response:\n", text_content)
                break

            consecutive_nudges = 0

            for tc_id, fn_name, args in tool_calls:
                if "_parse_error" in args:
                    tool_output = args["_parse_error"]
                elif fn_name == "scrape_website":
                    req_url = (args.get("url") or "").strip().rstrip("\\")
                    req_clean = req_url.split("#")[0].rstrip("/\\")
                    if strict_scroll_only and req_clean and req_clean != target_cache_url:
                        print(f"  [Blocked Off-URL] '{req_url[:60]}...' blocked in Strict Infinite Scroll Mode.")
                        tool_output = (
                            f"BLOCKED (STRICT INFINITE SCROLL MODE): '{url}' is the ONLY allowed URL. "
                            f"NEVER switch to /tag/ or other pages! {memory.get_next_unread_hint() or f'Use extract_with_selectors or scroll_and_scrape on {url}.'}"
                        )
                        consecutive_dups += 1
                    else:
                        tool_output = await scrape_website(
                            url=req_url,
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
                elif fn_name == "extract_with_selectors":
                    if selector_fail_streak >= 2:
                        tool_output = (
                            "SELECTOR TOOL DISABLED FOR THIS RUN (failed twice on obfuscated/complex DOM). "
                            f"{memory.get_next_unread_hint() or 'Read the cached page via scrape_website or open candidate URLs from [AGENT WORKING MEMORY] and save via save_to_csv.'}"
                        )
                        consecutive_dups += 1
                    else:
                        tool_output = await extract_with_selectors(
                            url=args.get("url") or url,
                            item_selector=args.get("item_selector") or "",
                            field_selectors=args.get("field_selectors") or {},
                            filename=csv_filename,
                            max_items=args.get("max_items") or (requested_count or 50),
                            mode=args.get("mode") or ("scroll" if strict_scroll_only else "single"),
                            unique_fields=args.get("unique_fields") or None,
                            append=saved_csv,
                            login_used=login_used,
                            login_skipped=login_skipped,
                            seen_calls=seen_calls,
                            memory=memory,
                        )
                        if "Saved " in str(tool_output):
                            saved_csv = True
                            consecutive_zero_saves = 0
                            selector_fail_streak = 0
                            consecutive_dups = 0
                        elif any(e in str(tool_output) for e in ("SELECTOR FAILED", "DUPLICATE SELECTOR BLOCKED", "REJECTED")):
                            selector_fail_streak += 1
                            consecutive_dups += 1
                        else:
                            consecutive_dups = 0
                elif fn_name == "scroll_and_scrape":
                    tool_output = await scroll_and_scrape(
                        url=args.get("url") or url,
                        max_scrolls=args.get("max_scrolls") or 12,
                        start_char=args.get("start_char") or 0,
                        seen_calls=seen_calls,
                        memory=memory,
                    )
                    if "DUPLICATE CALL BLOCKED" in str(tool_output):
                        consecutive_dups += 1
                    else:
                        consecutive_dups = 0
                elif fn_name == "ajax_paginate_and_scrape":
                    tool_output = await ajax_paginate_and_scrape(
                        url=args.get("url") or url,
                        max_pages=args.get("max_pages") or 6,
                        start_char=args.get("start_char") or 0,
                        seen_calls=seen_calls,
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
                        location=args.get("location") or "",
                        section=args.get("section") or "",
                        filters=args.get("filters") or None,
                        seen_calls=seen_calls,
                        memory=memory,
                    )
                    if "DUPLICATE SEARCH BLOCKED" in str(tool_output):
                        consecutive_dups += 1
                    else:
                        consecutive_dups = 0
                elif fn_name == "save_memory_note":
                    tool_output = memory.add_note(args.get("note") or "")
                    print(f"  [Memory Note] {args.get('note', '')[:80]}")
                    consecutive_dups = 0
                elif fn_name == "login_or_signup_with_email":
                    target_login_url = (args.get("url") or url).split("#")[0]
                    cached_page = PAGE_CACHE.get(target_login_url, "")
                    auth_required = True

                    if login_used or login_skipped or auth_failed:
                        tool_output = "Login was already handled in this run."
                    elif cached_page and is_404_page(cached_page):
                        print(f"  [Blocked Login on 404] '{target_login_url[:60]}...' is a broken page; skipping login request.")
                        tool_output = (
                            f"BLOCKED: '{target_login_url}' is a 404 / 'moved or deleted' page, NOT a login wall. "
                            "Pick another valid URL from [AGENT WORKING MEMORY]."
                        )
                    elif cached_page and not is_mandatory_login_wall(cached_page) and len(cached_page) >= 3500:
                        print(f"  [Skipped Unnecessary Login] Public content already visible ({len(cached_page)} chars).")
                        auth_required = False
                        tool_output = (
                            "BLOCKED: Mandatory login is NOT required on this page—public content is already available! "
                            "Use extract_with_selectors or scrape_website to extract the data."
                        )
                    else:
                        tool_output, was_skipped, verified = await login_or_signup_with_email(
                            url=args.get("url") or url,
                            seen_calls=seen_calls,
                            memory=memory,
                            username=args.get("username") or "",
                            password=args.get("password") or "",
                        )
                        if was_skipped:
                            login_skipped = True
                        elif verified:
                            login_used = True
                            auth_verified = True
                        else:
                            auth_failed = True
                        consecutive_dups = 0
                elif fn_name == "save_to_csv":
                    append_mode = _parse_append_flag(args.get("append")) if saved_csv else False
                    tool_output = save_to_csv(
                        filename=csv_filename,
                        items=args.get("items", []),
                        append=append_mode,
                        login_used=login_used,
                        login_skipped=login_skipped,
                        memory=memory,
                        unique_fields=args.get("unique_fields") or None,
                    )
                    if str(tool_output).startswith("Saved "):
                        saved_csv = True
                        consecutive_zero_saves = 0
                    elif "[ZERO-PROGRESS ERROR]" in str(tool_output):
                        consecutive_zero_saves += 1
                else:
                    tool_output = f"Unknown tool: {fn_name}"

                if "[ANTI-BOT BLOCKED" in str(tool_output):
                    anti_bot_hits += 1

                tool_msg: dict = {"role": "tool", "content": str(tool_output)}
                if LLM_PROVIDER == "ollama":
                    tool_msg["tool_name"] = fn_name
                else:
                    tool_msg["tool_call_id"] = tc_id
                messages.append(tool_msg)

            if auth_failed:
                print("\n[AUTH FAILED] Mandatory authentication could not be verified. Aborting without silent fallback.")
                break

            if anti_bot_hits >= 3 and not saved_csv and not memory.visited_urls and not memory.pending_urls:
                print("\n[Stopped] Target website is blocking automated access (anti-bot/CAPTCHA). Exiting cleanly.")
                break

            if consecutive_zero_saves >= 2:
                unread_hint = memory.get_next_unread_hint()
                if unread_hint:
                    print("\n[Loop Breaker] Repeated 0-new-row save_to_csv blocked. Forcing next unread start_char...")
                    messages.append({
                        "role": "user",
                        "content": (
                            f"STOP calling save_to_csv with duplicate items! {unread_hint} "
                            "Or open unvisited URLs from [AGENT WORKING MEMORY]."
                        ),
                    })
                    consecutive_zero_saves = 0
                else:
                    print("\n[Stopped] Repeated 0-new-row save_to_csv with no unread content left. Exiting cleanly.")
                    break

            if consecutive_dups >= 3:
                if dup_redirect_count < 2:
                    dup_redirect_count += 1
                    print("\n[Loop Breaker] Repeated duplicate/failed calls prevented. Redirecting agent...")
                    fallback_hint = (
                        memory.get_next_unread_hint()
                        or (f"Call search_on_website(url='{memory.visited_urls[-1]}', query='...') to search for the requested items." if memory.visited_urls and not saved_csv else "")
                        or "Pick an unvisited URL from [AGENT WORKING MEMORY] with scrape_website, or save valid extracted items using save_to_csv."
                    )
                    messages.append({
                        "role": "user",
                        "content": f"STOP repeating failed selectors or duplicate scrape calls! {fallback_hint}",
                    })
                    consecutive_dups = 0
                else:
                    print("\n[Stopped] Agent repeated duplicate/failing calls. Exiting cleanly.")
                    break

            if LLM_PROVIDER == "groq":
                await asyncio.sleep(3)
    finally:
        await BROWSER_MANAGER.close(clear_auth=True)

    if auth_failed:
        auth_status = "FAILED (verification failed)"
    elif auth_verified:
        auth_status = "VERIFIED (authenticated session)"
    elif login_skipped:
        auth_status = "SKIPPED by user"
    elif auth_required:
        auth_status = "REQUIRED (not completed)"
    else:
        auth_status = "NOT REQUIRED (public page)"

    if not saved_csv or not os.path.exists(csv_filename) or memory.total_saved_count == 0:
        ext_status = "FAILED (NO DATA SAVED)"
    elif requested_count and memory.total_saved_count < requested_count:
        ext_status = (
            f"PARTIAL SUCCESS ({memory.total_saved_count}/{requested_count} unique rows saved -> "
            f"{os.path.abspath(csv_filename)})"
        )
    else:
        ext_status = f"SUCCESS ({memory.total_saved_count} unique rows saved -> {os.path.abspath(csv_filename)})"

    print(f"\n--- Run Summary (after {step} steps) ---")
    print(f"Authentication Status : {auth_status}")
    print(f"Extraction Status     : {ext_status}")

    return {
        "steps": step,
        "auth_status": auth_status,
        "extraction_status": ext_status,
        "rows_saved": memory.total_saved_count,
        "csv_path": os.path.abspath(csv_filename),
    }