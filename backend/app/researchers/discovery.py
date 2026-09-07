"""Enumerate stable public identities; the sitemap is a cross-check, not an identity key."""

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from app.researchers.client import ORIGIN, SEARCH_URL, ImportFailure


def identity(source: dict) -> dict:
    alias = source.get("profilepagealias")
    source_id = source.get("id")
    if not isinstance(source_id, int) or source_id <= 0 or not isinstance(alias, str):
        raise ImportFailure("Directory entry lacks a stable identity")
    if not re.fullmatch(r"[\w.-]+", alias) or alias in {".", ".."}:
        raise ImportFailure("Directory profile alias is invalid")
    return {
        "id": source_id,
        "network_id": source.get("networkuserid"),
        "alias": alias,
        "display_name": source.get("fullnamewithtitle_secondary") or alias,
        "source_url": f"{ORIGIN}/{alias}",
    }


async def discover(client) -> tuple[list[dict], dict]:
    found, reported = {}, None
    for offset in range(0, 100000, 50):
        page = await client.fetch(
            SEARCH_URL,
            query={
                "query": {"bool": {"must": [{"term": {"type_secondary.keyword": "User"}}]}},
                "sort": [{"title_order": "asc"}, {"name.keyword.sort": "asc"}, {"surname.keyword.sort": "asc"}],
                "from": offset,
                "size": 50,
                "_source": ["id", "networkuserid", "profilepagealias", "fullnamewithtitle_secondary"],
            },
        )
        try:
            data = json.loads(page.body)
            total = data["hits"]["total"]
            total = total["value"] if isinstance(total, dict) else total
            hits = data["hits"]["hits"]
            if data.get("timed_out") or data.get("_shards", {}).get("failed", 0):
                raise ImportFailure("Directory returned partial search results")
            if not isinstance(total, int) or total <= 0 or not isinstance(hits, list):
                raise ImportFailure("Empty or invalid directory response")
            if reported is not None and reported != total:
                raise ImportFailure("Directory count changed during discovery; retry discovery")
            reported = total
            if offset % 500 == 0:
                print(f"Directory discovery: {offset}/{total} entries", flush=True)
            before = len(found)
            for hit in hits:
                item = identity(hit["_source"])
                if item["id"] in found:
                    raise ImportFailure("Directory repeated an identity across pages; retry discovery")
                found[item["id"]] = item
            if len(found) >= reported:
                break
            if len(found) == before or len(hits) < 50:
                raise ImportFailure("Directory pagination ended before the reported total")
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ImportFailure):
                raise
            raise ImportFailure("Unrecognized directory JSON; discovery aborted") from None
    if len(found) != reported:
        raise ImportFailure("Directory count does not match unique identities")
    aliases = {item["alias"].casefold() for item in found.values()}
    if len(aliases) != len(found):
        raise ImportFailure("Multiple researcher IDs share an alias; identity review required")
    sitemap = await client.fetch(ORIGIN + "/cvpages.xml")
    try:
        root = ET.fromstring(sitemap.body)
        urls = [node.text for node in root.iter() if node.tag.endswith("}loc") and node.text]
        if not urls:
            raise ImportFailure("Profile sitemap is empty")
    except ET.ParseError:
        raise ImportFailure("Invalid profile sitemap") from None
    sitemap_aliases = {
        urlsplit(url).path.strip("/").casefold() for url in urls if urlsplit(url).hostname == "avesis.metu.edu.tr"
    }
    return list(found.values()), {
        "reported_total": reported,
        "unique_identities": len(found),
        "sitemap_urls": len(urls),
        "directory_only": sorted(aliases - sitemap_aliases),
        "sitemap_only": sorted(sitemap_aliases - aliases),
        "complete": True,
    }
