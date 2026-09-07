"""Extract public English academic content without translating or executing page code."""

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.researchers.client import ImportFailure, StopImport

SECTIONS = {
    "educations",
    "topics",
    "experiences",
    "publications",
    "projects",
    "scientificactivities",
    "achievements",
    "documents",
    "contact",
    "lessons",
    "courses",
    "theses",
    "research",
}
ACTIVITY = re.compile(
    r"^/(?:publication/details|project/details|patent/details|advisingthesis/details|"
    r"publication|project|patent|yayin|proje|yonetilen-tez|award|oduller|design|tasarimlar)/"
)
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def canonical(url: str) -> str:
    p = urlsplit(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", query, ""))


def text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def links(node, base: str) -> list[dict]:
    result = {}
    for anchor in node.select("a[href]"):
        href = urljoin(base, anchor["href"])
        if urlsplit(href).scheme not in {"http", "https", "mailto", "tel"}:
            continue
        url = canonical(href)
        result[url] = {"url": url, "label": text(anchor)}
    return list(result.values())


@dataclass
class Parsed:
    content: dict
    sections: dict[str, str]
    pagination: list[str]
    details: list[str]
    fields: dict


def parse_page(body: bytes, url: str, alias: str, *, detail=False, expected_id=None) -> Parsed:
    soup = BeautifulSoup(body, "html.parser")
    title = text(soup.title) if soup.title else ""
    if re.search(r"captcha|access denied|just a moment|verify you are human", title, re.I) or soup.select_one(
        '.g-recaptcha, #challenge-form, iframe[src*="captcha"]'
    ):
        raise StopImport("AVESIS challenge page encountered")
    language = (soup.html.get("lang", "") if soup.html else "").lower()
    if not language.startswith("en"):
        raise ImportFailure("AVESIS did not return the English view")
    heading = soup.select_one("h1")
    main = soup.select_one("#main-area")
    if detail and main is None:
        main = soup.select_one("main, .body .container, #main")
    if main is None or (not detail and heading is None):
        raise ImportFailure("Unrecognized AVESIS page layout")
    photo = soup.select_one('img[src*="/user/image/"]')
    if expected_id is not None and photo:
        match = re.search(r"/user/image/(\d+)", photo.get("src", ""))
        if match and int(match[1]) != expected_id:
            raise ImportFailure("Profile identity differs from directory ID")
    all_links = links(soup, url)
    fields = {}
    if not detail:
        display = text(heading)
        match = re.match(r"^((?:Asst\. Prof\.|Assoc\. Prof\.|Res\. Asst\.|Prof\.|Lect\.|Dr\.)\s*)(.*)$", display)
        fields = {
            "name": match[2] if match else display,
            "title": match[1].strip() if match else None,
            "photo_url": urljoin(url, photo["src"]) if photo else None,
        }
        institution = soup.select_one(".corporateInformation")
        fields["affiliation"] = text(institution) if institution else None
        email = soup.select_one('a[href^="mailto:"]')
        fields["email"] = email["href"].removeprefix("mailto:").split("?")[0] if email else None
    sections = {}
    menu = soup.select_one("#primary-menu")
    for link in links(menu, url) if menu else []:
        path = urlsplit(link["url"])
        parts = path.path.strip("/").split("/")
        if path.hostname != "avesis.metu.edu.tr" or len(parts) != 2 or parts[0] != alias:
            continue
        if parts[1] in {"download", "indir"} or path.query:
            continue
        sections[parts[1]] = link["url"]
    pagination = []
    for anchor in main.select('.pagination a[href], a[rel="next"]'):
        candidate = canonical(urljoin(url, anchor["href"]))
        p, origin = urlsplit(candidate), urlsplit(url)
        if p.netloc == origin.netloc and p.path == origin.path and candidate != canonical(url):
            pagination.append(candidate)
    for element in main.select("script,style,noscript,nav,.pagination"):
        element.decompose()
    items = {}
    for item in main.select(".pub-item, .timeline-item, .tl-item"):
        item_links = links(item, url)
        primary = next((link["url"] for link in item_links if ACTIVITY.match(urlsplit(link["url"]).path)), None)
        body_text = re.sub(r"^\d+\.\s*", "", text(item))
        if not body_text:
            continue
        key_match = UUID.search(primary or "")
        key = key_match[0].lower() if key_match else primary or digest(body_text)
        items[key] = {"key": key, "text": body_text, "links": item_links}
    main_links = links(main, url)
    details = (
        sorted(
            {
                link["url"]
                for link in main_links
                if urlsplit(link["url"]).hostname == "avesis.metu.edu.tr" and ACTIVITY.match(urlsplit(link["url"]).path)
            }
        )
        if not detail
        else []
    )
    metrics = {}
    for label in main.select(".metrics-title"):
        count = label.parent.select_one(".metrics-count")
        if count:
            raw = text(count).replace(",", "")
            metrics[text(label)] = int(raw) if raw.isdigit() else text(count)
    labels = {}
    for label in main.select("strong"):
        key = text(label).rstrip(":")
        sibling = label.find_next_sibling("span")
        if sibling and key and len(key) < 100:
            labels[key] = text(sibling)
    warnings = []
    section = urlsplit(url).path.strip("/").split("/")[-1]
    if not detail and section != alias and section not in SECTIONS:
        warnings.append("Unrecognized section retained as visible text and links")
    if main.select('[data-toggle="asyncmodal"], .moreMetrics'):
        warnings.append("Additional interactive panels are linked but not expanded")
    content = {
        "heading": text(soup.select_one("#h2-page-title")) if soup.select_one("#h2-page-title") else title,
        "text": text(main),
        "fields": labels,
        "metrics": metrics,
        "items": list(items.values()),
        "links": main_links,
        "profile_links": all_links if not detail and section == alias else [],
        "warnings": warnings,
    }
    # Empty known sections are legitimate. Missing page structure was rejected above.
    return Parsed(content, sections, list(dict.fromkeys(pagination)), details, fields)


def combine(pages: list[dict]) -> dict:
    result = dict(pages[0])
    result["text"] = "\n".join(dict.fromkeys(page["text"] for page in pages))
    result["items"] = list({item["key"]: item for page in pages for item in page["items"]}.values())
    result["links"] = list({link["url"]: link for page in pages for link in page["links"]}.values())
    result["warnings"] = sorted({warning for page in pages for warning in page["warnings"]})
    return result
