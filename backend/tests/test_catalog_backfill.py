from app.academic_catalog.backfill import cache_key, recover


def test_backfill_keeps_terms_separate_and_rejects_plausible_wrong_identity():
    rows = []
    for term in ("20252", "20261"):
        values = {"department": "240", "semester": term, "course": "2402201"}
        rows.append({"key_hash": cache_key("get_course_info", values),
                     "payload": {"semester": term, "course_code": "2402201", "sections": []}})
    wrong = {"key_hash": cache_key("get_course_info", {"department": "240", "semester": "20261", "course": "2402202"}),
             "payload": {"semester": "20261", "course_code": "2402201", "sections": []}}
    accepted, unresolved, _ = recover([*rows, wrong])
    assert {row["values"]["semester"] for row in accepted} == {"20252", "20261"}
    assert len(unresolved) == 1
    assert unresolved[0]["key_hash"] == wrong["key_hash"]


def test_listing_term_must_be_recovered_by_exact_hash_not_assumed_from_dates():
    listing = {"key_hash": cache_key("list_program_courses", {"department": "240", "semester": "20261"}),
               "payload": {"result": [{"course_code": "2402201", "name": "History"}]}}
    assert recover([listing])[0] == []
    accepted, unresolved, _ = recover([listing], ["20252", "20261"])
    assert not unresolved
    assert accepted[0]["values"] == {"department": "240", "semester": "20261"}
