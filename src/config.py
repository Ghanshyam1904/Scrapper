import os
from dotenv import load_dotenv
from groq import Groq
from ollama import Client as OllamaClient
from openai import OpenAI

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------- PATHS ----------------
USER_DATA_DIR = os.path.join(BASE_DIR, "browser_profile")
MEMORY_FILE = os.path.join(BASE_DIR, "agent_memory.json")
DATA_DIR = os.path.join(BASE_DIR, "data")

# ---------------- ENV CONFIG ----------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b").strip()
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", os.path.join(BASE_DIR, "system_prompt.txt")).strip()
if not os.path.isabs(SYSTEM_PROMPT_FILE):
    SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, SYSTEM_PROMPT_FILE)
DEFAULT_LOGIN_EMAIL = os.getenv("LOGIN_EMAIL", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# Har LLM call mein ye limit lagani hai (128000 wali error se bachne ke liye)
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "4000"))

GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]

# ---------------- FREELLMAPI ----------------
FREELLM_BASE_URL = os.getenv("FREELLM_BASE_URL", "http://localhost:3001/v1").strip()
FREELLM_MODEL = os.getenv("FREELLM_MODEL", "nemotron-3-ultra-550b").strip()
# Pehla model fail/rate-limit ho to ye try honge
FREELLM_FALLBACK_MODELS = list(dict.fromkeys([
    FREELLM_MODEL,          # nemotron-3-ultra-550b
    "gpt-oss-120b",
    "llama-3.3-70b",
    "command-a",
]))

# freellm fail ho to Ollama par girne ke liye uska client bhi chahiye
ollama_client = OllamaClient() if LLM_PROVIDER in ("ollama", "freellm") else None
groq_client = None
freellm_client = None

if LLM_PROVIDER == "groq":
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key or groq_key.startswith("gsk_aapki"):
        raise ValueError("GROQ_API_KEY is not set properly in the .env file!")
    groq_client = Groq(api_key=groq_key)
elif LLM_PROVIDER == "freellm":
    freellm_key = os.getenv("FREELLM_API_KEY", "").strip()
    if not freellm_key:
        raise ValueError("FREELLM_API_KEY is not set in the .env file!")
    freellm_client = OpenAI(base_url=FREELLM_BASE_URL, api_key=freellm_key, timeout=180)
elif LLM_PROVIDER != "ollama":
    raise ValueError(f"Unsupported LLM_PROVIDER='{LLM_PROVIDER}'. Use 'ollama', 'groq' or 'freellm'.")

PAGE_CACHE: dict[str, str] = {}
DOM_SAMPLE_CACHE: dict[str, str] = {}

# ---------------- DETECTION PHRASES ----------------
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
    "you are human",
    "your are human",
    "are you a human",
    "robot or human",
    "activate and hold",
    "confirm that you’re human",
    "confirm that you're human",
    "confirm you are not a robot",
    "confirm you're human",
    "verify you're human",
    "sorry, we just need to make sure you're not a robot",
    "click the button below to continue shopping",
    "enter the characters you see below",
    "type the characters you see in this image",
    "solve this puzzle",
    "robot check",
    "press & hold",
    "access denied",
    "403 forbidden",
    "enable javascript and cookies",
    "unusual traffic from your computer",
    "attention required! | cloudflare",
    "pardon our interruption",
    "blocked by network security",
    "whoa there, pardner",
    "geo.captcha-delivery.com",
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

LINKEDIN_GATE_MARKERS = (
    "welcome to your professional community",
    "agree & join",
    "already on linkedin?",
    "sign in to linkedin",
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a precision autonomous web scraping AI agent. "
    "Always call scrape_website first on the Target URL. "
    "When [DOM Structure Sample] shows repeating items, prefer extract_with_selectors for fast verbatim extraction. "
    "If [PAGINATION DETECTED] or clickable Next/page buttons exist, call extract_with_selectors(..., mode='ajax' or 'url_pagination') or ajax_paginate_and_scrape. "
    "If the page uses infinite scrolling, call extract_with_selectors(..., mode='scroll') or scroll_and_scrape. "
    "If the Target URL lacks the requested topic/items and has a search bar, call search_on_website. "
    "Use exact user-requested column names and save incrementally via save_to_csv."
)

DISMISS_POPUPS_JS = """
(() => {
    try {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27, bubbles: true }));
        const closeSelectors = [
            "button[aria-label*='close' i]",
            "button[aria-label*='dismiss' i]",
            "[class*='close' i][role='button']",
            "button._2KpZ6l._2doB4z",
            "span._30XB9F",
            "#onetrust-accept-btn-handler"
        ];
        for (const sel of closeSelectors) {
            try {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                    el.click();
                }
            } catch (e) {}
        }
        const dismissLabels = new Set(['no thanks', 'skip', 'not now', 'close', 'dismiss', 'accept all', 'got it']);
        for (const btn of document.querySelectorAll("button, [role='button']")) {
            try {
                if (!btn || btn.offsetParent === null) continue;
                const txt = (btn.innerText || '').trim().toLowerCase();
                if (txt.length > 0 && txt.length <= 20 && dismissLabels.has(txt)) {
                    btn.click();
                }
            } catch (e) {}
        }
        document.querySelectorAll('a[title]').forEach(a => {
            const t = (a.innerText || '').trim();
            const full = (a.getAttribute('title') || '').trim();
            if (full.length > t.length && (t.endsWith('...') || t.endsWith('…'))) {
                a.innerText = full;
            }
        });
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
        return os.path.join(BASE_DIR, norm)
    return os.path.join(DATA_DIR, norm)


def load_system_prompt(filepath: str) -> str:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return DEFAULT_SYSTEM_PROMPT