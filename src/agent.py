import asyncio
import os
import re

from src.config import (
    LLM_PROVIDER,
    SYSTEM_PROMPT_FILE,
    PAGE_CACHE,
    load_system_prompt,
    resolve_csv_path,
)
from src.memory import WorkingMemory
from src.browser import is_404_page, is_mandatory_login_wall
from src.llm import build_tools, trim_messages, call_llm, get_active_model
from src.tools import (
    scrape_website,
    search_on_website,
    login_or_signup_with_email,
    save_to_csv,
)


async def run_agent(url: str, user_goal: str, csv_filename: str, max_steps: int = 30):
    count_match = re.search(
        r"\b(?:first|top|at least)\s+(\d+)\b|\b(\d+)\s+(?:countries|records|items|products|jobs|books|quotes|articles|reviews|rows)\b",
        user_goal,
        re.IGNORECASE,
    )
    requested_count = int(count_match.group(1) or count_match.group(2)) if count_match else None

    csv_filename = resolve_csv_path(csv_filename)
    PAGE_CACHE.clear()
    seen_calls: set[tuple] = set()
    memory = WorkingMemory(url, requested_count=requested_count)
    system_prompt = load_system_prompt(SYSTEM_PROMPT_FILE)
    tools = build_tools()

    print(f"\n[Agent Started] Provider: {LLM_PROVIDER.upper()} | Model: {get_active_model()} | Output: {csv_filename}")

    full_prompt = (
        f"Target URL: {url}\n"
        f"User Requirement (extract ONLY what is asked): {user_goal}\n"
        f"Save extracted records into CSV file: '{csv_filename}'.\n"
        "First call scrape_website on the Target URL. If the Target URL is a homepage and the requested topic/items/jobs are not there, "
        "call search_on_website (pass 'section', 'location', and 'filters' if the user requested specific tabs, locations, or filters). "
        "Use save_memory_note to hold partial facts while reading large pages, "
        "use keyword to jump straight to relevant sections, and call save_to_csv immediately after EACH completed item."
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
        print(f"\n--- Agent Step {step} ({get_active_model()}) ---")
        safe_messages = trim_messages(messages, memory=memory)
        history_msg, text_content, tool_calls = call_llm(safe_messages, tools, step)
        messages.append(history_msg)

        if not tool_calls:
            incomplete = bool(
                requested_count
                and memory.total_saved_count < requested_count
                and not login_skipped
            )
            if (not saved_csv or incomplete) and not login_skipped and nudge_count < 3:
                nudge_count += 1
                if not saved_csv:
                    print(f"  [Guardrail] '{csv_filename}' has not been created yet; prompting agent to save ({nudge_count}/3)...")
                    nudge_msg = (
                        f"You have NOT called save_to_csv yet and '{csv_filename}' was not created. "
                        "Check [AGENT WORKING MEMORY] and save valid items NOW."
                    )
                else:
                    print(f"  [Guardrail] Incomplete ({memory.total_saved_count}/{requested_count} saved); prompting agent to continue ({nudge_count}/3)...")
                    nudge_msg = (
                        f"INCOMPLETE EXTRACTION: User requested {requested_count} unique records, "
                        f"but only {memory.total_saved_count} are saved in CSV so far! "
                        f"Read the next unread chunk using start_char or next unvisited URL from [AGENT WORKING MEMORY] "
                        f"and save the remaining {requested_count - memory.total_saved_count} unique records using exact headers {memory.saved_headers}. "
                        "Only stop if the source truly has fewer total records."
                    )
                messages.append({"role": "user", "content": nudge_msg})
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
                    location=args.get("location") or "",
                    section=args.get("section") or "",
                    filters=args.get("filters") or None,
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
                elif cached_page and is_404_page(cached_page):
                    print(f"  [Blocked Login on 404] '{target_login_url[:60]}...' is a broken page; skipping login request.")
                    tool_output = (
                        f"BLOCKED: '{target_login_url}' is a 404 / 'moved or deleted' page, NOT a login wall. "
                        "Pick another valid URL from [AGENT WORKING MEMORY]."
                    )
                elif cached_page and not is_mandatory_login_wall(cached_page) and len(cached_page) >= 3500:
                    print(f"  [Skipped Unnecessary Login] Public content already visible ({len(cached_page)} chars).")
                    tool_output = (
                        "BLOCKED: Mandatory login is NOT required on this page—public content is already available! "
                        "Use scrape_website(keyword=...) or start_char (or search_on_website) to read and extract the data."
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
                if str(tool_output).startswith("Saved ") or (memory is not None and memory.total_saved_count > 0):
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
                        "Pick an unvisited URL from [AGENT WORKING MEMORY], try search_on_website, "
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