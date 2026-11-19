import os
from dotenv import load_dotenv
from groq import Groq
from ollama import Client as OllamaClient

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------- PATHS ----------------
USER_DATA_DIR = os.path.join(BASE_DIR, "browser_profile")
MEMORY_FILE = os.path.join(BASE_DIR, "agent_memory.json")
DATA_DIR = os.path.join(BASE_DIR, "data")

# ---------------- ENV CONFIG ----------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b").strip()
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", os.path.join(BASE_DIR, "system_prompt.txt")).strip()
if not os.path.isabs(SYSTEM_PROMPT_FILE):
    SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, SYSTEM_PROMPT_FILE)
DEFAULT_LOGIN_EMAIL = os.getenv("LOGIN_EMAIL", "").strip()

GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]

ollama_client = OllamaClient() if LLM_PROVIDER == "ollama" else None
groq_client = None
if LLM_PROVIDER == "groq":
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key or groq_key.startswith("gsk_aapki"):
        raise ValueError("GROQ_API_KEY is not set properly in the .env file!")
    groq_client = Groq(api_key=groq_key)
elif LLM_PROVIDER != "ollama":
    raise ValueError(f"Unsupported LLM_PROVIDER='{LLM_PROVIDER}'. Use 'ollama' or 'groq'.")

# Shared in-run page cache
PAGE_CACHE: dict[str, str] = {}

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

LINKEDIN_GATE_MARKERS = (
    "welcome to your professional community",
    "agree & join",
    "already on linkedin?",
    "sign in to linkedin",
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a precision autonomous web scraping AI agent. "
    "Always call scrape_website first on the Target URL and read through its contents before opening sub-links. "
    "If the user asks for a topic, product, or job not present on a homepage, call search_on_website "
    "(pass 'section', 'location', and 'filters' when requested). "
    "Use save_memory_note to store partial facts while searching for remaining fields. "
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
        return os.path.join(BASE_DIR, norm)
    return os.path.join(DATA_DIR, norm)


def load_system_prompt(filepath: str) -> str:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return DEFAULT_SYSTEM_PROMPT