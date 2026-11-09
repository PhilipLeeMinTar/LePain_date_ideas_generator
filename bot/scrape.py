"""Fetch a listing page and turn it into compact text for the LLM."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_CHARS = 45_000
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-SG,en;q=0.9",
}


def fetch(url: str, timeout: int = 30) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        log.warning("fetch failed %s: %s", url, e)
        return None
    if resp.status_code != 200:
        log.warning("fetch %s -> HTTP %s", url, resp.status_code)
        return None
    resp.encoding = resp.encoding or resp.apparent_encoding
    return resp.text


def _jsonld_events(soup: BeautifulSoup) -> list[dict]:
    """Pull schema.org Event objects many event sites embed for SEO."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.endswith("Event") for x in types):
                keep = {k: node.get(k) for k in ("name", "startDate", "endDate", "url", "location", "offers", "description")}
                if isinstance(keep.get("description"), str):
                    keep["description"] = keep["description"][:300]
                found.append(keep)
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def html_to_text(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    jsonld = _jsonld_events(soup)

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav"]):
        tag.decompose()

    # Keep links inline so the LLM can attach the right URL to each event.
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(base_url, a["href"])
        if text and href.startswith("http"):
            a.replace_with(f"{text} <{href}>")

    title = soup.title.get_text(strip=True) if soup.title else ""
    body = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in body.splitlines()]
    text = "\n".join(ln for ln in lines if ln)

    parts = [f"PAGE TITLE: {title}", f"PAGE URL: {base_url}"]
    if jsonld:
        parts.append("STRUCTURED EVENTS (JSON-LD):\n" + json.dumps(jsonld, ensure_ascii=False, default=str)[:15_000])
    parts.append("PAGE TEXT:\n" + text)
    return "\n\n".join(parts)[:MAX_CHARS]
