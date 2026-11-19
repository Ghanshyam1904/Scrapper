import json
import os
import re
from urllib.parse import urlparse
from src.config import MEMORY_FILE


def _load_site_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _save_site_memory(data: dict) -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().lstrip("www.")


class WorkingMemory:
    def __init__(self, target_url: str, requested_count: int | None = None):
        self.target_url = target_url.split("#")[0]
        self.domain = _domain_of(target_url)
        self.requested_count = requested_count
        self.visited_urls: list[str] = []
        self.broken_urls: list[str] = []
        self.pending_urls: list[str] = []
        self.notes: list[str] = []
        self.saved_summaries: list[str] = []
        self.saved_headers: list[str] = []
        self.total_saved_count: int = 0
        self.site_memory: dict = _load_site_memory()

    def mark_visited(self, url: str) -> None:
        u = url.split("#")[0]
        if u not in self.visited_urls:
            self.visited_urls.append(u)
        if u in self.pending_urls:
            self.pending_urls.remove(u)

    def mark_broken(self, url: str) -> None:
        u = url.split("#")[0]
        if u not in self.broken_urls:
            self.broken_urls.append(u)
        if u in self.pending_urls:
            self.pending_urls.remove(u)

    def extract_candidate_urls(self, text: str, source_url: str) -> None:
        raw_urls = re.findall(r"https?://[^\s)\"'>]+", text)
        ignore_parts = (
            "/login", "/signin", "/signup", "/ap/signin", "/ap/register",
            "/authwall", "/checkpoint/", "/uas/login",
            "/help", "/privacy", "/terms", "/cart", "/wishlist", "/account",
            "/category/", "/categories/", "/tag/",
            "ratings-reviews-details-page", ".png", ".jpg", ".jpeg", ".webp",
            ".svg", ".css", ".js", "amazon.com/gp/goldbox",
        )
        src_clean = source_url.split("#")[0].rstrip("/")
        for u in raw_urls:
            u_clean = u.split("#")[0].rstrip(".,;:")
            low = u_clean.lower()
            if _domain_of(u_clean) != self.domain:
                continue
            if u_clean.rstrip("/") == src_clean or u_clean in self.visited_urls or u_clean in self.broken_urls:
                continue
            if any(bad in low for bad in ignore_parts):
                continue
            path = urlparse(u_clean).path or "/"
            is_interesting = (
                "/dp/" in low
                or "/p/itm" in low
                or "/product-reviews/" in low
                or "/jobs/view/" in low
                or "/answer/" in low
                or "/article" in low
                or "/blog" in low
                or (path.count("/") >= 1 and len(path) >= 20)
            )
            if is_interesting and u_clean not in self.pending_urls:
                self.pending_urls.append(u_clean)
                if len(self.pending_urls) >= 12:
                    break

    def add_note(self, note: str) -> str:
        note = note.strip()
        if not note:
            return "Empty note ignored."
        if note not in self.notes:
            self.notes.append(note)
            if len(self.notes) > 10:
                self.notes = self.notes[-10:]
        return f"Memory note saved ({len(self.notes)} total notes)."

    def record_saved_rows(self, items: list[dict], headers: list[str], total_count: int) -> None:
        self.saved_headers = list(headers)
        self.total_saved_count = total_count
        for item in items:
            compact = ", ".join(f"{k}={str(v)[:50]}" for k, v in item.items())
            self.saved_summaries.append(compact)

    def remember_keyword(self, keyword: str) -> None:
        kw = keyword.strip()
        if not kw or not self.domain:
            return
        entry = self.site_memory.setdefault(self.domain, {})
        kws = entry.setdefault("working_keywords", [])
        if kw not in kws:
            kws.append(kw)
            entry["working_keywords"] = kws[-5:]
            _save_site_memory(self.site_memory)

    def remember_login_required(self, required: bool) -> None:
        if not self.domain:
            return
        entry = self.site_memory.setdefault(self.domain, {})
        entry["mandatory_login_seen"] = required
        _save_site_memory(self.site_memory)

    def render(self) -> str:
        lines = ["[AGENT WORKING MEMORY — ALWAYS UP TO DATE]"]
        domain_info = self.site_memory.get(self.domain, {})
        if domain_info.get("working_keywords"):
            lines.append(f"- Known useful keywords on {self.domain}: {', '.join(domain_info['working_keywords'])}")

        target_str = f" / {self.requested_count} requested" if self.requested_count else ""
        lines.append(f"- Unique rows saved to CSV: {self.total_saved_count}{target_str}")
        if self.saved_headers:
            lines.append(f"- LOCKED CSV headers (must match exactly): {', '.join(self.saved_headers)}")

        if self.saved_summaries:
            lines.append("- Latest saved rows:")
            for idx, s in enumerate(self.saved_summaries[-6:], 1):
                lines.append(f"  {idx}. {s}")

        if self.notes:
            lines.append("- Saved scratchpad notes:")
            for n in self.notes:
                lines.append(f"  * {n}")

        if self.pending_urls:
            lines.append("- Other discovered item/article URLs (ONLY visit if the current page does NOT already have the data):")
            for idx, u in enumerate(self.pending_urls[:8], 1):
                lines.append(f"  {idx}. {u}")

        if self.visited_urls:
            lines.append(f"- Already visited URLs ({len(self.visited_urls)}): {', '.join(u[:75] for u in self.visited_urls[-5:])}")
        if self.broken_urls:
            lines.append(f"- Broken/404 URLs (DO NOT retry): {', '.join(u[:75] for u in self.broken_urls[-4:])}")
        return "\n".join(lines)
