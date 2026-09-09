"""Conservative reuse of the existing catalog-to-AVESIS matching approach."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from sqlalchemy import select

from app.admin.directory import METU_ID
from app.researchers.models import Researcher

_TITLES = re.compile(r"^(?:(?:asst|assoc|res|prof|dr|lect|assist|assistant|associate)\.?\s+)+", re.I)
_PLACEHOLDERS = {"", "staff", "tba", "tbd", "unknown", "belirlenmedi", "ogretim elemani"}


def tokens(name: str) -> tuple[str, ...]:
    name = _TITLES.sub("", name.strip()).translate(str.maketrans({"İ": "i", "I": "ı"})).casefold()
    return tuple(re.findall(r"[^\W\d_]+", unicodedata.normalize("NFC", name)))


def folded(words):
    return tuple("".join(c for c in unicodedata.normalize("NFKD", word.replace("ı", "i"))
                         if not unicodedata.combining(c)) for word in words)


class ResearcherNames:
    def __init__(self, researchers):
        self.exact = defaultdict(set)
        self.folded = defaultdict(set)
        for researcher_id, name in researchers:
            words = tokens(name)
            if len(words) < 2:
                continue
            for offset in range(len(words)):
                rotated = words[offset:] + words[:offset]
                self.exact[rotated].add(researcher_id)
                self.folded[folded(rotated)].add(researcher_id)

    def match(self, name):
        words = tokens(name)
        result = {"researcher_id": None, "match_method": "unmatched", "candidate_ids": []}
        if " ".join(folded(words)) in _PLACEHOLDERS:
            return {**result, "resolution_status": "unassigned"}
        exact = self.exact.get(words, set())
        candidates = sorted(exact | self.folded.get(folded(words), set()))
        if len(candidates) == 1:
            return {"researcher_id": candidates[0], "candidate_ids": candidates, "resolution_status": "matched",
                    "match_method": "full_name_rotation" if exact else "transliterated_rotation"}
        return {**result, "candidate_ids": candidates, "resolution_status": "ambiguous" if candidates else "unmatched"}


async def enrich_instructors(db, organization_id, sections: list[dict]) -> list[dict]:
    # The current researcher table contains the METU source dataset without an
    # organization column. Do not expose its identities to another organization.
    rows = (await db.execute(select(Researcher.id, Researcher.name))).all() if organization_id == METU_ID else []
    names = ResearcherNames(rows)
    enriched = []
    for section in sections:
        instructors = []
        for source in section.get("instructors", []):
            item = dict(source) if isinstance(source, dict) else {"source_name": str(source)}
            name = item.get("source_name", "")
            # Existing explicit admin identities are overrides, never rematched.
            if not item.get("researcher_id"):
                item.update(names.match(name))
            instructors.append(item)
        enriched.append({**section, "instructors": instructors})
    return enriched
