"""English Wikipedia search, reading, and private-to-Nit knowledge review."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import uuid

from cat.capabilities import CapabilityResult

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "InternetCat/1.0 (personal simulation; Wikipedia reading feature)"}


def _get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read(2_000_000).decode("utf-8"))


class Search:
    name = "wikipedia_search"
    external = True

    def run(self, ctx, inputs):
        query = str(inputs.get("query", "")).strip()[:200]
        if not query:
            return CapabilityResult(ok=False, text="There is nothing to search for yet.")
        data = _get({"action": "query", "list": "search", "srsearch": query, "srlimit": 5,
                     "srprop": "snippet", "format": "json", "formatversion": 2})
        results = []
        for row in data.get("query", {}).get("search", []):
            title = row.get("title", "")
            results.append({"id": str(uuid.uuid4()), "pageid": row.get("pageid"), "title": title,
                            "snippet": row.get("snippet", ""), "query": query, "searched_at": ctx.sim_time,
                            "url": "https://en.wikipedia.org/?curid=" + str(row.get("pageid", "")),
                            "status": "unread"})
        ops = [{"op": "append_raw", "fact": "terminal.searches",
                "value": {"id": str(uuid.uuid4()), "query": query, "searched_at": ctx.sim_time,
                          "result_count": len(results),
                          "results": [{"title": x["title"], "url": x["url"]} for x in results]}}]
        ops.extend({"op": "append_raw", "fact": "terminal.results", "value": item} for item in results)
        titles = ", ".join(x["title"] for x in results)
        text = (f"Wikipedia search returned {len(results)} pages" + (f": {titles}." if titles else ". Try another phrase if you like."))
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
        data = _get({"action": "query", "pageids": str(result["pageid"]), "prop": "extracts",
                     "explaintext": 1, "exsectionformat": "plain", "format": "json", "formatversion": 2})
        pages = data.get("query", {}).get("pages", [])
        extract = (pages[0].get("extract", "") if pages else "")[:6000]
        if not extract:
            extract = "Wikipedia did not provide readable page text."
        article = {"id": result["id"], "title": result["title"], "url": result["url"],
                   "query": result.get("query", ""), "read_at": ctx.sim_time,
                   "text": extract, "status": "read", "meaning": ""}
        ops = [{"op": "update_item_raw", "fact": "terminal.results", "id": result["id"],
                "fields": {"status": "read", "read_at": ctx.sim_time}},
               {"op": "append_raw", "fact": "terminal.articles", "value": article}]
        return CapabilityResult(text=f"You read ‘{result['title']}’ on Wikipedia.\n\n{extract}",
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

    def run(self, ctx, inputs):
        operation = inputs.get("operation")
        handler = {"search": Search(), "read": Read(), "review": Review()}.get(operation)
        if handler is None:
            return CapabilityResult(ok=False, text="The terminal could not complete that request.")
        return handler.run(ctx, inputs)


CAPABILITY = Wikipedia()
