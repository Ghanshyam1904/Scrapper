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
        parent = os.path.dirname(MEMORY_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _domain_of(url: str) -> str:
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    return urlparse(u).netloc.lower().lstrip("www.")


def _same_site(url_domain: str, target_domain: str) -> bool:
    if not url_domain or not target_domain:
        return False
    return (
        url_domain == target_domain
        or url_domain.endswith("." + target_domain)
        or target_domain.endswith("." + url_domain)
    )


def _norm_url(url: str) -> str:
    return (url or "").strip().split("#")[0].rstrip("/")


class WorkingMemory:
    def __init__(self, target_url: str, requested_count: int | None = None):
        self.target_url = (target_url or "").strip().split("#")[0]
        self.domain = _domain_of(self.target_url)
        self.requested_count = requested_count
        self.visited_urls: list[str] = []
        self.broken_urls: list[str] = []
        self.pending_urls: list[str] = []
        self.notes: list[str] = []
        self.saved_summaries: list[str] = []
        self.saved_headers: list[str] = []
        self.total_saved_count: int = 0
        self.next_unread_offsets: dict[str, tuple[int, int]] = {}
        self.site_memory: dict = _load_site_memory()

    def _is_visited(self, url: str) -> bool:
        nu = _norm_url(url)
        return any(_norm_url(v) == nu for v in self.visited_urls)

    def _is_broken(self, url: str) -> bool:
        nu = _norm_url(url)
        return any(_norm_url(b) == nu for b in self.broken_urls)

    def mark_visited(self, url: str) -> None:
        u = _norm_url(url)
        if u and not self._is_visited(u):
            self.visited_urls.append(u)
        self.pending_urls = [p for p in self.pending_urls if _norm_url(p) != u]

    def mark_broken(self, url: str) -> None:
        u = _norm_url(url)
        if u and not self._is_broken(u):
            self.broken_urls.append(u)
        self.pending_urls = [p for p in self.pending_urls if _norm_url(p) != u]
        for k in list(self.next_unread_offsets.keys()):
            if _norm_url(k) == u:
                self.next_unread_offsets.pop(k, None)

    def record_chunk_read(self, url: str, next_offset: int, total_len: int) -> None:
        u = (url or "").strip().split("#")[0]
        if not u or self._is_broken(u):
            return
        prev_off, _ = self.next_unread_offsets.get(u, (0, total_len))
        self.next_unread_offsets[u] = (max(prev_off, int(next_offset)), int(total_len))

    def clear_root_unread(self) -> None:
        for u in list(self.next_unread_offsets.keys()):
            parsed = urlparse(u)
            if parsed.path.strip("/") == "" and not parsed.query:
                self.next_unread_offsets.pop(u, None)

    def get_next_unread_hint(self) -> str:
        for u, (off, tot) in reversed(list(self.next_unread_offsets.items())):
            if self._is_broken(u):
                continue
            if off < tot:
                return f"Call scrape_website(url='{u}', start_char={off}) to read unread chars {off}..{tot}."
        return ""

    def extract_candidate_urls(self, text: str, source_url: str) -> None:
        if "/scroll" in self.target_url.lower():
            return

        raw_urls = re.findall(r"https?://[^\s)\"'>\]\\]+", text or "")
        ignore_parts = (
            "/login", "/signin", "/signup", "/logout", "/register", "/forgot-password",
            "/ap/signin", "/ap/register", "/authwall", "/checkpoint/", "/uas/login",
            "/help", "/privacy", "/terms", "/cookie", "/cart", "/wishlist", "/account",
            "ratings-reviews-details-page", ".png", ".jpg", ".jpeg", ".webp",
            ".svg", ".css", ".js", ".ico", ".woff", ".ttf", ".pdf", ".mp4",
            "amazon.com/gp/goldbox",
        )
        high_signal_patterns = (
            "/dp/", "/p/itm", "/itm/", "/ip/", "/product-reviews/", "/products/",
            "/jobs/view/", "/viewjob", "/job-listing", "/job-listings",
            "/hotel/", "/hotel_review", "/restaurant_review", "/biz/",
            "/wiki/", "/answer/", "/comments/", "/catalogue/", "/page/",
        )
        medium_signal_patterns = (
            "/jobs/", "/article", "/blog", "/news/", "/post/", "/story/", "/review",
        )

        src_norm = _norm_url(source_url)
        existing_norm = {_norm_url(p) for p in self.pending_urls}
        scored_candidates: list[tuple[int, str]] = []

        for u in raw_urls:
            u_clean = u.split("#")[0].rstrip(".,;:!?")
            u_norm = _norm_url(u_clean)
            low = u_clean.lower()

            if not _same_site(_domain_of(u_clean), self.domain):
                continue
            if not u_norm or u_norm == src_norm or self._is_visited(u_norm) or self._is_broken(u_norm):
                continue
            if u_norm in existing_norm:
                continue
            if any(bad in low for bad in ignore_parts):
                continue

            parsed = urlparse(u_clean)
            path = parsed.path or "/"

            if any(p in low for p in high_signal_patterns) or re.search(r"[?&](?:page|page_num|pg|p|id)=\d+", low):
                score = 3
            elif any(p in low for p in medium_signal_patterns):
                score = 2
            elif path.count("/") >= 1 and len(path.strip("/")) >= 6:
                score = 1
            else:
                continue

            existing_norm.add(u_norm)
            scored_candidates.append((score, u_clean))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        for _, u_clean in scored_candidates:
            if len(self.pending_urls) >= 20:
                break
            self.pending_urls.append(u_clean)

    def add_note(self, note: str) -> str:
        note = (note or "").strip()
        if not note:
            return "Empty note ignored."
        if note not in self.notes:
            self.notes.append(note)
            if len(self.notes) > 10:
                self.notes = self.notes[-10:]
        return f"Memory note saved ({len(self.notes)} total notes)."

    def record_saved_rows(self, items: list[dict], headers: list[str], total_count: int) -> None:
        self.saved_headers = list(headers)
        self.total_saved_count = int(total_count)
        for item in items:
            compact = ", ".join(f"{k}={str(v)[:50]}" for k, v in item.items())
            self.saved_summaries.append(compact)
        if len(self.saved_summaries) > 20:
            self.saved_summaries = self.saved_summaries[-20:]

    def remember_keyword(self, keyword: str) -> None:
        kw = (keyword or "").strip()
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
        entry["mandatory_login_seen"] = bool(required)
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

        unread = self.get_next_unread_hint()
        if unread:
            lines.append(f"- Unread content available: {unread}")

        if self.saved_summaries:
            lines.append("- Latest saved rows:")
            for idx, s in enumerate(self.saved_summaries[-5:], 1):
                lines.append(f"  {idx}. {s}")

        if self.notes:
            lines.append("- Saved scratchpad notes:")
            for n in self.notes:
                lines.append(f"  * {n}")

        if self.pending_urls:
            lines.append("- Other discovered item/pagination URLs:")
            for idx, u in enumerate(self.pending_urls[:8], 1):
                lines.append(f"  {idx}. {u}")

        if self.visited_urls:
            lines.append(f"- Already visited URLs ({len(self.visited_urls)}): {', '.join(u[:75] for u in self.visited_urls[-5:])}")
        if self.broken_urls:
            lines.append(f"- Broken/404 URLs (DO NOT retry): {', '.join(u[:75] for u in self.broken_urls[-4:])}")
        return "\n".join(lines)