import json
import time
from groq import APIStatusError

from src.config import (
    LLM_PROVIDER,
    OLLAMA_MODEL,
    GROQ_FALLBACK_MODELS,
    ollama_client,
    groq_client,
)
from src.memory import WorkingMemory

CURRENT_GROQ_IDX = 0


def get_active_model() -> str:
    if LLM_PROVIDER == "ollama":
        return OLLAMA_MODEL
    return GROQ_FALLBACK_MODELS[CURRENT_GROQ_IDX % len(GROQ_FALLBACK_MODELS)]


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
                    "Search for a topic, job, article, or product using the website's own search bar. "
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