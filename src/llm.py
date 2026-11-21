import asyncio
import json
from groq import APIStatusError

from src.config import (
    LLM_PROVIDER,
    OLLAMA_MODEL,
    GROQ_FALLBACK_MODELS,
    FREELLM_FALLBACK_MODELS,
    MAX_OUTPUT_TOKENS,
    ollama_client,
    groq_client,
    freellm_client,
)
from src.memory import WorkingMemory

CURRENT_GROQ_IDX = 0
CURRENT_FREELLM_IDX = 0


def get_active_model() -> str:
    if LLM_PROVIDER == "ollama":
        return OLLAMA_MODEL
    if LLM_PROVIDER == "freellm":
        return FREELLM_FALLBACK_MODELS[CURRENT_FREELLM_IDX % len(FREELLM_FALLBACK_MODELS)]
    return GROQ_FALLBACK_MODELS[CURRENT_GROQ_IDX % len(GROQ_FALLBACK_MODELS)]


def build_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "save_table_rows",
                "description": "Save rows straight from the HTML table reported by [HTML TABLE FOUND]. Values are copied by code, never typed by you.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "columns": {"type": "object",
                                    "description": "csv column name -> column index shown in the preview"},
                        "first_data_row": {"type": "integer",
                                           "description": "row_index of the first data row (skip header rows)"},
                        "max_items": {"type": "integer"},
                    },
                    "required": ["url", "columns", "first_data_row", "max_items"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_jsonld_list",
                "description": "Save the full list found in a page's JSON-LD ItemList straight to CSV. Use when scrape_website reports [STRUCTURED LIST FOUND].",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_items": {"type": "integer"},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scrape_website",
                "description": (
                    "Fetch clean text, structured tables/JSON-LD, links, and a [DOM Structure Sample] from a URL. "
                    "Use 'start_char' to read unread offsets or 'keyword' to jump to sections."
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
                "name": "extract_with_selectors",
                "description": (
                    "FASTEST tool for repeating lists/cards/rows when you know exact CSS selectors from [DOM Structure Sample]. "
                    "Use standard CSS selectors only (no :has-text). Supports ::attr(href) syntax. "
                    "If selectors are obfuscated or fail twice, use scrape_website + save_to_csv instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full URL starting with https://"},
                        "item_selector": {"type": "string", "description": "CSS selector for each item container (e.g. '.quote', 'div.thumbnail', 'tr')"},
                        "field_selectors": {
                            "type": "object",
                            "description": "Map of exact requested CSV column name -> CSS selector inside item_selector",
                        },
                        "max_items": {"type": "integer", "description": "Exact number of unique records requested (e.g. 50, 30)"},
                        "mode": {
                            "type": "string",
                            "description": "One of: 'single' (current page), 'scroll' (infinite scroll), 'ajax' (clickable Next/numbered buttons), 'url_pagination'",
                        },
                        "unique_fields": {
                            "type": "array",
                            "description": "Optional composite key columns for deduplication (e.g. ['quote', 'author'], ['team_name', 'year'])",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["url", "item_selector", "field_selectors", "max_items", "mode"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scroll_and_scrape",
                "description": (
                    "Perform real browser infinite scrolling on dynamic pages (works on both window and nested scroll containers). "
                    "Pass start_char > 0 to read later chunks of an already-scrolled page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full URL starting with https://"},
                        "max_scrolls": {"type": "integer", "description": "Maximum scroll steps to perform (default 12)"},
                        "start_char": {"type": "integer", "description": "Character offset to return (default 0)"},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ajax_paginate_and_scrape",
                "description": (
                    "Click interactive pagination buttons ('Next', '2', '3', 'Load more') across multiple pages in one browser session, "
                    "wait for DOM updates on each page, and return all collected pages cleanly."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full URL starting with https://"},
                        "max_pages": {"type": "integer", "description": "Maximum number of pages to click through (default 6)"},
                        "start_char": {"type": "integer", "description": "Character offset to return (default 0)"},
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
                    "Search for a topic, job, article, hotel, or product using the website's own search bar. "
                    "Supports optional section tab (e.g. 'Jobs'), location (e.g. 'India'), and UI filters (e.g. ['Past 24 hours', 'Entry level'])."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The website URL (e.g. homepage) to search on"},
                        "query": {"type": "string", "description": "Search query (e.g. 'Machine Learning Engineer')"},
                        "location": {"type": "string", "description": "Optional location (e.g. 'India')"},
                        "section": {"type": "string", "description": "Optional tab/section to open before searching (e.g. 'Jobs')"},
                        "filters": {
                            "type": "array",
                            "description": "Optional filter labels to apply (e.g. ['Past 24 hours', 'Entry level'])",
                            "items": {"type": "string"},
                        },
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
                    "Save temporary scratchpad facts into working memory while browsing further down a page. "
                    "NEVER use this as a replacement for save_to_csv. Memory notes do NOT write to CSV."
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
                "description": (
                    "Save extracted items into CSV immediately after finding them on a page. "
                    "Use the EXACT snake_case column names requested by the user."
                ),
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
                        "unique_fields": {
                            "type": "array",
                            "description": "Optional composite key column names for deduplication (e.g. ['quote', 'author'], ['team_name', 'year']).",
                            "items": {"type": "string"},
                        },
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
                    "Only pass 'username' and 'password' if authorized demo credentials are explicitly printed on the page; otherwise leave them empty."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The login/signup or blocked page URL"},
                        "username": {"type": "string", "description": "Optional authorized demo username/email printed on the page"},
                        "password": {"type": "string", "description": "Optional authorized demo password printed on the page"},
                    },
                    "required": ["url"],
                },
            },
        },
    ]


def _compact_assistant_msg(msg: dict) -> dict:
    tcs = msg.get("tool_calls")
    if not tcs:
        return msg

    compact_tcs = []
    changed = False
    for tc in tcs:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "")
        is_dict_args = isinstance(raw_args, dict)
        args_len = len(json.dumps(raw_args)) if is_dict_args else len(str(raw_args))

        if fn.get("name") == "save_to_csv" and args_len > 500:
            changed = True
            note_payload = {"note": "older save_to_csv payload compacted; see [AGENT WORKING MEMORY]"}
            new_tc = {
                "type": tc.get("type", "function"),
                "function": {
                    "name": "save_to_csv",
                    "arguments": note_payload if is_dict_args else json.dumps(note_payload),
                },
            }
            if "id" in tc:
                new_tc["id"] = tc["id"]
            compact_tcs.append(new_tc)
        else:
            compact_tcs.append(tc)

    if not changed:
        return msg
    new_msg = dict(msg)
    new_msg["tool_calls"] = compact_tcs
    return new_msg


def trim_messages(messages: list, memory: WorkingMemory | None = None) -> list:
    safe = list(messages)
    if memory is not None and safe and isinstance(safe[0], dict) and safe[0].get("role") == "system":
        safe[0] = {
            "role": "system",
            "content": safe[0]["content"] + "\n\n" + memory.render(),
        }

    if len(safe) <= 7:
        return safe

    trimmed = []
    cutoff = len(safe) - 4
    for i, msg in enumerate(safe):
        if 2 <= i < cutoff and isinstance(msg, dict):
            if msg.get("role") == "tool" and len(msg.get("content", "")) > 450:
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
            elif msg.get("role") == "assistant":
                trimmed.append(_compact_assistant_msg(msg))
            else:
                trimmed.append(msg)
        else:
            trimmed.append(msg)
    return trimmed


def _parse_tool_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"_parse_error": "Tool arguments must be a JSON object."}
    except json.JSONDecodeError:
        return {"_parse_error": f"Invalid JSON arguments: {raw}"}


def _messages_for_ollama(messages: list) -> list:
    """OpenAI-style messages (string arguments) ko Ollama ke dict-arguments format mein badlo."""
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("tool_calls"):
            m = dict(m)
            fixed = []
            for tc in m["tool_calls"]:
                fn = dict(tc.get("function", {}))
                fn["arguments"] = _parse_tool_args(fn.get("arguments"))
                if "_parse_error" in fn["arguments"]:
                    fn["arguments"] = {}
                fixed.append({"type": "function", "function": fn})
            m["tool_calls"] = fixed
        out.append(m)
    return out


async def _call_ollama(safe_messages: list, tools: list, step: int):
    response = await asyncio.to_thread(
        ollama_client.chat,
        model=OLLAMA_MODEL,
        messages=_messages_for_ollama(safe_messages),
        tools=tools,
        think=False,
        options={"num_ctx": 12288, "temperature": 0.0},
    )
    msg = response.message
    history_msg: dict = {"role": "assistant", "content": msg.content or ""}
    normalized_calls = []

    if msg.tool_calls:
        ollama_tcs = []
        for idx, tc in enumerate(msg.tool_calls):
            args = _parse_tool_args(tc.function.arguments)
            ollama_tcs.append({
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": args if "_parse_error" not in args else {},
                },
            })
            normalized_calls.append((f"ollama_{step}_{idx}", tc.function.name, args))
        history_msg["tool_calls"] = ollama_tcs

    return history_msg, (msg.content or ""), normalized_calls


def _parse_openai_style(response):
    """Groq aur FreeLLMAPI dono ka response same format mein hota hai."""
    msg = response.choices[0].message
    history_msg = {"role": "assistant", "content": msg.content or ""}
    normalized_calls = []

    if msg.tool_calls:
        history_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            for tc in msg.tool_calls
        ]
        for tc in msg.tool_calls:
            normalized_calls.append((tc.id, tc.function.name, _parse_tool_args(tc.function.arguments)))

    return history_msg, (msg.content or ""), normalized_calls


async def _call_freellm(safe_messages: list, tools: list, step: int):
    global CURRENT_FREELLM_IDX
    n = len(FREELLM_FALLBACK_MODELS)
    last_err = None

    for _ in range(n * 2):
        model_name = FREELLM_FALLBACK_MODELS[CURRENT_FREELLM_IDX % n]
        try:
            response = await asyncio.to_thread(
                freellm_client.chat.completions.create,
                model=model_name,
                messages=safe_messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.0,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            if not response.choices:
                raise RuntimeError("Empty response")
            return _parse_openai_style(response)
        except Exception as e:
            last_err = e
            status = getattr(e, "status_code", None)
            text = str(e).lower()

            # Saari keys/limits khatam -> turant Ollama par girao
            if status == 503 and "all models exhausted" in text:
                print("  [FreeLLMAPI: all models exhausted] Falling back to Ollama...")
                break
            if status == 429:
                print(f"  [Rate Limit 429 on {model_name}] Next model...")
            elif status in (400, 404, 422):
                print(f"  [{model_name} request reject kiya ({status}), shayad tools support nahi] Next model...")
            elif status and status >= 500:
                print(f"  [Server Error {status} on {model_name}] Next model...")
            else:
                print(f"  [{model_name} error: {str(e)[:100]}] Next model...")
            CURRENT_FREELLM_IDX += 1
            await asyncio.sleep(1)

    if ollama_client is None:
        raise RuntimeError(f"FreeLLMAPI fail hua aur Ollama client nahi hai: {last_err}")
    print(f"  [Ollama fallback: {OLLAMA_MODEL}]")
    return await _call_ollama(safe_messages, tools, step)


async def call_llm(safe_messages: list, tools: list, step: int):
    global CURRENT_GROQ_IDX

    if LLM_PROVIDER == "ollama":
        return await _call_ollama(safe_messages, tools, step)

    if LLM_PROVIDER == "freellm":
        return await _call_freellm(safe_messages, tools, step)

    for _ in range(len(GROQ_FALLBACK_MODELS) + 3):
        model_name = GROQ_FALLBACK_MODELS[CURRENT_GROQ_IDX % len(GROQ_FALLBACK_MODELS)]
        try:
            response = await asyncio.to_thread(
                groq_client.chat.completions.create,
                model=model_name,
                messages=safe_messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.0,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
            return _parse_openai_style(response)
        except APIStatusError as e:
            if e.status_code == 429 and "tokens per day" in str(e).lower():
                print(f"  [TPD Full on {model_name}] Switching to next free Groq model...")
                CURRENT_GROQ_IDX += 1
            elif e.status_code == 413:
                print(f"  [Payload Too Large 413 on {model_name}] Switching model...")
                CURRENT_GROQ_IDX += 1
            elif e.status_code == 429:
                print(f"  [Rate Limit 429 on {model_name}] Waiting 20s...")
                await asyncio.sleep(20)
            elif e.status_code >= 500:
                print(f"  [Server Error {e.status_code} on {model_name}] Waiting 5s...")
                await asyncio.sleep(5)
            else:
                raise
    raise RuntimeError("All Groq fallback models hit rate limits. Switch LLM_PROVIDER=ollama in .env!")