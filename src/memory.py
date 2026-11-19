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
    def __init__(self, target_url: str):
        self.target_url = target_url.split("#")[0]
        self.domain = _domain_of(target_url)
        self.visited_urls: list[str] = []
        self.broken_urls: list[str] = []
        self.pending_urls: list[str] = []
        self.notes: list[str] = []
        self.saved_summaries: list[str] = []
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

    def record_saved_rows(self, items: list[dict]) -> None:
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
        if self.saved_summaries:
            lines.append(f"- Already saved to CSV ({len(self.saved_summaries)} rows):")
            for idx, s in enumerate(self.saved_summaries[-8:], 1):
                lines.append(f"  {idx}. {s}")
        else:
            lines.append("- Already saved to CSV: 0 rows")

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