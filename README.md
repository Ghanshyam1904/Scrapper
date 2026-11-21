# Universal AI Web Scraping Agent

An autonomous, LLM-powered web scraping agent built with **Crawl4AI**, **Playwright**, and **Ollama / Groq**.  
Given any website URL and a natural-language extraction prompt, the agent navigates pages, searches inside sites, handles anti-bot protection, prompts for login **only when mandatory**, remembers progress across steps, and saves clean structured records into `data/*.csv`.

---

## Features

- **Dual LLM Support (Local & Cloud):**
  - **Ollama** (default: `qwen3.5:4b`) for free local execution.
  - **Groq** (`openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`) with automatic rate-limit fallback for complex multi-page tasks.
- **Anti-Bot & Stealth Fetching:**
  - Runs headless Crawl4AI with stealth flags, popup/modal auto-dismissal, and `<nav>` / `<aside>` / `<footer>` noise filtering.
  - Automatically falls back to a headed Chrome/Edge Playwright browser when Cloudflare, Akamai, or JS challenges block headless scraping.
- **Mandatory-Only Login Flow:**
  - Ignores optional header `"Login"` buttons and dismissible login popups (e.g., Flipkart modals).
  - Triggers `login_or_signup_with_email` **only** when a mandatory sign-in gate blocks access (e.g., Quora), captures the unlocked session, and reuses cookies via `./browser_profile`.
- **Universal In-Site Search (`search_on_website`):**
  - Automatically locates search bars on homepages (Amazon, Flipkart, Quora, etc.), submits queries, and captures the search results page.
- **2-Layer Agent Memory:**
  - **Working Memory (`WorkingMemory` + `save_memory_note`):** Tracks unvisited URLs, visited/broken URLs, scratchpad notes, and CSV save progress across context trimming.
  - **Persistent Site Memory (`agent_memory.json`):** Remembers useful search keywords and login requirements per domain across runs.
- **Broken SPA Route & 404 Protection:**
  - Detects 404 / deleted pages without wasting login prompts and auto-resolves broken SPA review links (such as Flipkart's `ratings-reviews-details-page` → `/product-reviews/`).
- **Validated Incremental CSV Storage (`data/`):**
  - Rejects placeholder rows (`"N/A"`, `"Not publicly visible"`, etc.) unless login was skipped, and saves all outputs automatically inside `data/`.

---

## Project Structure

```text
scrapper/
├── main.py                  # CLI entry point
├── system_prompt.txt        # Customizable agent instructions
├── requirements.txt         # Python dependencies
├── .env                     # Environment configuration (create from .env.example)
├── .env.example             # Sample environment variables
├── data/                    # Auto-created folder for all output CSVs
├── browser_profile/         # Auto-created persistent Chrome/Playwright session
├── agent_memory.json        # Auto-created per-domain persistent memory
└── src/
    ├── __init__.py
    ├── config.py            # Paths, .env config, LLM clients, detection rules, PAGE_CACHE
    ├── memory.py            # WorkingMemory & agent_memory.json manager
    ├── browser.py           # Stealth browser launcher, DOM markdown extractor, URL/wall detectors
    ├── tools.py             # scrape_website, search_on_website, login_or_signup_with_email, save_to_csv
    ├── llm.py               # Ollama/Groq unified caller, tool schemas, trim_messages
    └── agent.py             # Core run_agent() execution loop
```

---

## Setup & Installation

### 1. Create and activate a virtual environment

```bash
python -m venv myenv

# Windows (PowerShell / Command Prompt):
myenv\Scripts\activate

# macOS / Linux:
source myenv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt

# Install Playwright browsers (required for headless/stealth scraping):
#   playwright install chromium
#   # Or, to install all browsers:
#   playwright install
```

### 3. Configure `.env`

Copy `.env.example` to `.env` and set your preferred provider:

#### Option A — Local with Ollama (Default)

```env
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3.5:4b
SYSTEM_PROMPT_FILE=system_prompt.txt
LOGIN_EMAIL=
```

Make sure Ollama is running and the model is pulled:

```bash
ollama pull qwen3.5:4b
```

#### Option B — Cloud with Groq (Recommended for multi-product scraping)

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_actual_groq_api_key
SYSTEM_PROMPT_FILE=system_prompt.txt
LOGIN_EMAIL=your_email@example.com
```

---

## Usage

### Verify modules
Test that all dependencies and modules load cleanly:

```bash
python -c "import src.config, src.memory, src.browser, src.tools, src.llm, src.agent; print('ALL MODULES OK')"
```

### Run the Agent

```bash
python main.py
```

Enter the details when prompted:

```text
=== Universal AI Web Scraping Agent ===
Paste Website URL: https://books.toscrape.com/
What do you want to extract? Extract top 5 book titles, prices, and availability only.
Output CSV filename (saved in 'data/', Enter for 'output.csv'): books.csv
```

All saved CSV files are placed inside `data/` (e.g., `data/books.csv`).

---

## Example Test Prompts

| Website | URL | Prompt | Expected Behavior |
| :--- | :--- | :--- | :--- |
| **Books to Scrape** | `https://books.toscrape.com/` | Extract top 5 book titles, prices, and availability only. | Direct extraction, no login required |
| **Hacker News** | `https://news.ycombinator.com/` | Extract top 5 news titles, points, and author usernames only. | Ignores header login link, extracts and saves to CSV |
| **Flipkart** | `https://www.flipkart.com/search?q=narzo+100+lite+5g` | Open the first mobile and extract model_name, price, rating, and 3 reviews with reviewer_name. | Dismisses popup, auto-fixes review URL, extracts without login |
| **Amazon** | `https://www.amazon.in/` | Search Asus Vivobook 16 and extract model, price, and rating for top 3 laptops. | Uses `search_on_website` + WorkingMemory across products |
| **Quora** | `https://www.quora.com/` | Search Machine Learning and extract top 3 article descriptions and author names only. | Prompts Chrome login once for mandatory wall, reuses session thereafter |
| **LinkedIn** | `https://www.linkedin.com/` | Go to Jobs, search Machine Learning Engineer in India with filters Past 24 hours and Entry level. Extract top 5 job titles, company names, locations, and job links. | Opens Jobs section, types location, applies filters, bypasses authwall/login if needed, saves clean URLs to CSV |

---

## Available Agent Tools

* **`scrape_website(url, start_char=0, keyword="")`**: Fetches clean markdown/text with Crawl4AI + stealth Playwright fallback; supports keyword jumps and offset browsing.
* **`search_on_website(url, query)`**: Finds and interacts with the site's search bar to jump straight to search results.
* **`save_memory_note(note)`**: Stores partial extracted facts in `WorkingMemory` while scanning large pages.
* **`login_or_signup_with_email(url)`**: Opens a persistent Chrome window for manual login/OTP/CAPTCHA only when a mandatory gate blocks content.
* **`save_to_csv(filename, items, append=True)`**: Validates and writes real extracted rows into `data/<filename>.csv`.

---

## Troubleshooting

* **Multi-product tasks stop early on `qwen3.5:4b`**: Small 4B models can struggle with long multi-step reasoning. Switch `LLM_PROVIDER=groq` in `.env` or pull a larger Ollama model (e.g., `qwen2.5:14b`).
* **Stale or corrupted browser session**: Delete the `./browser_profile` folder and re-run `python main.py` to initialize a fresh Chrome profile.
* **Customizing extraction behavior**: Edit `system_prompt.txt` directly without modifying any Python source code.
