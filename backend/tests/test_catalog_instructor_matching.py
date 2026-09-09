from app.academic_catalog.instructors import ResearcherNames


def test_full_names_rotate_but_initials_and_placeholders_do_not_match():
    names = ResearcherNames([(1, "Özcan Yazıcı"), (2, "Ayşe Demir Kaya")])
    assert names.match("YAZICI ÖZCAN")["researcher_id"] == 1
    assert names.match("Demir Kaya Ayşe")["researcher_id"] == 2
    assert names.match("STAFF")["resolution_status"] == "unassigned"
    assert names.match("Ö. Yazıcı")["researcher_id"] is None


def test_transliteration_collision_stays_ambiguous_even_with_exact_match():
    names = ResearcherNames([(1, "Özcan Yazıcı"), (2, "Ozcan Yazici")])
    result = names.match("YAZICI ÖZCAN")
    assert result["researcher_id"] is None
    assert result["resolution_status"] == "ambiguous"
    assert result["candidate_ids"] == [1, 2]
