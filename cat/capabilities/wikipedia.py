"""English Wikipedia search, reading, and private-to-Nit knowledge review, with guards (docs/rewrite-contracts.md §13.2).

Guards: the web is outside its world, so a network error, rate limit or timeout is never *its* failure (the action is
turned away without costing it confidence and without teaching it a false belief, and is not offered again for a while);
a page's words are untrusted data (stripped of markup, quoted and labelled, capped, never an instruction); queries and
titles are checked against a configurable blocklist (content/capabilities.yaml `wikipedia.blocklist`); requests are
polite (one at a time, maxlag, a short timeout, the lead section only).
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cat.capabilities import CapabilityResult

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "InternetCat/1.0 (personal simulation; Wikipedia reading feature)"}
TIMEOUT = 8
READ_CHARS = 2500            # the lead section kept with the page
SHOWN_CHARS = 1600           # what reaches its prompt (the prompt budget is small)
BACKOFF = (120, 300, 900)    # real seconds it is left alone after consecutive failures
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAGS = re.compile(r"<[^>]+>")

_failures = 0
_down_until = 0.0


class Unavailable(Exception):
    """The library cannot be reached right now (network, rate limit, bad answer)."""


def unavailable_now() -> bool:
    return time.monotonic() < _down_until


def _failed() -> None:
    global _failures, _down_until
    _down_until = time.monotonic() + BACKOFF[min(_failures, len(BACKOFF) - 1)]
    _failures += 1


def _worked() -> None:
    global _failures, _down_until
    _failures, _down_until = 0, 0.0


def clean(text: str, limit: int) -> str:
    """Words from the web as plain text: no markup, no control characters, one line of spaces, shortened."""
    text = html.unescape(_TAGS.sub("", text or ""))
    return " ".join(_CONTROL.sub("", text).split())[:limit]


def blocked(ctx, *texts: str) -> bool:
    words = [str(w).lower() for w in ((ctx.content.capabilities.get("wikipedia") or {}).get("blocklist") or [])]
    low = " ".join(texts).lower()
    return any(w and w in low for w in words)


def _get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode({**params, "maxlag": 5})
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read(1_000_000).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:      # HTTPError is a URLError
        raise Unavailable(str(e)) from e
    if isinstance(data, dict) and data.get("error"):                              # for example maxlag: the servers are busy
        raise Unavailable(str(data["error"])[:120])
    return data


class Search:
    name = "wikipedia_search"
    external = True

    def run(self, ctx, inputs):
        query = clean(str(inputs.get("query", "")), 200)
        if not query:
            return CapabilityResult(ok=False, text="There is nothing to search for yet.")
        if blocked(ctx, query):
            return CapabilityResult(text=ctx.texts["search_nothing"], fields={"count": 0}, external=True)
        data = _get({"action": "query", "list": "search", "srsearch": query, "srlimit": 5,
                     "srprop": "snippet", "format": "json", "formatversion": 2})
        results = []
        for row in data.get("query", {}).get("search", []):
            title = clean(row.get("title", ""), 120)
            if not title or blocked(ctx, title):
                continue
            results.append({"id": str(uuid.uuid4()), "pageid": row.get("pageid"), "title": title,
                            "snippet": clean(row.get("snippet", ""), 300), "query": query, "searched_at": ctx.sim_time,
                            "url": "https://en.wikipedia.org/?curid=" + str(row.get("pageid", "")),
                            "status": "unread"})
        ops = [{"op": "append_raw", "fact": "terminal.searches",
                "value": {"id": str(uuid.uuid4()), "query": query, "searched_at": ctx.sim_time,
                          "result_count": len(results),
                          "results": [{"title": x["title"], "url": x["url"]} for x in results]}}]
        ops.extend({"op": "append_raw", "fact": "terminal.results", "value": item} for item in results)
        titles = ", ".join(f"\u2018{x['title']}\u2019" for x in results)
        text = (f"Wikipedia search returned {len(results)} pages: {titles}." if results
                else ctx.texts["search_nothing"])
        return CapabilityResult(text=text, fields={"count": len(results)}, world=ops, external=True)


class Read:
    name = "wikipedia_read"
    external = True

    def run(self, ctx, inputs):
        chosen = str(inputs.get("page", ""))
        result = next((x for x in ctx.facts.get("terminal.results", [])
                       if isinstance(x, dict) and x.get("id") == chosen), None)
        if not result:
            return CapabilityResult(ok=False, text="That search result is no longer available.")
        data = _get({"action": "query", "pageids": str(result["pageid"]), "prop": "extracts", "exintro": 1,
                     "explaintext": 1, "exchars": READ_CHARS, "exsectionformat": "plain", "format": "json",
                     "formatversion": 2})
        pages = data.get("query", {}).get("pages", [])
        extract = clean(pages[0].get("extract", "") if pages else "", READ_CHARS)
        if not extract or blocked(ctx, extract[:400]):
            extract = "Wikipedia did not provide readable page text."
        article = {"id": result["id"], "title": result["title"], "url": result["url"],
                   "query": result.get("query", ""), "read_at": ctx.sim_time,
                   "text": extract, "status": "read", "meaning": ""}
        ops = [{"op": "update_item_raw", "fact": "terminal.results", "id": result["id"],
                "fields": {"status": "read", "read_at": ctx.sim_time}},
               {"op": "append_raw", "fact": "terminal.articles", "value": article}]
        shown = extract if len(extract) <= SHOWN_CHARS else extract[: SHOWN_CHARS - 3].rstrip() + "..."
        return CapabilityResult(text=ctx.texts["page_read"].format(title=result["title"], text=shown),
                                fields={"title": result["title"]}, world=ops, external=True)


class Review:
    name = "wikipedia_review"
    external = True

    def run(self, ctx, inputs):
        article_id = str(inputs.get("article", ""))
        decision = str(inputs.get("decision", "discarded"))
        if decision not in ("kept", "discarded"):
            decision = "discarded"
        meaning = str(inputs.get("meaning", "")).strip()[:1000] if decision == "kept" else ""
        summary = str(inputs.get("summary", "")).strip()[:1200] if decision == "kept" else ""
        article = next((x for x in ctx.facts.get("terminal.articles", [])
                        if isinstance(x, dict) and x.get("id") == article_id and x.get("status") == "read"), None)
        if not article:
            return CapabilityResult(ok=False, text="That page has already been reviewed.")
        fields = {"status": decision, "reviewed_at": ctx.sim_time, "meaning": meaning, "summary": summary}
        text = ("You kept the page as knowledge and considered what it means to you."
                if decision == "kept" else "You set the page aside without keeping it as knowledge.")
        return CapabilityResult(text=text, world=[{"op": "update_item_raw", "fact": "terminal.articles",
                                                    "id": article_id, "fields": fields}], external=False)


class Wikipedia:
    name = "wikipedia"
    external = False

    def unavailable_now(self) -> bool:                       # the registry will not offer the actions while this holds
        return unavailable_now()

    def run(self, ctx, inputs):
        operation = inputs.get("operation")
        handler = {"search": Search(), "read": Read(), "review": Review()}.get(operation)
        if handler is None:
            return CapabilityResult(ok=False, text="The terminal could not complete that request.")
        if operation != "review" and unavailable_now():
            return CapabilityResult(ok=False, unavailable=True, text=ctx.texts["terminal_dark"])
        try:
            res = handler.run(ctx, inputs)
        except Unavailable:
            _failed()
            return CapabilityResult(ok=False, unavailable=True, text=ctx.texts["terminal_dark"])
        if operation != "review":
            _worked()
        return res


CAPABILITY = Wikipedia()
