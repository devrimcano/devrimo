"""Context selection has to answer the same questions with less, not less well.

Every dependency field is re-sent on every model step of a run, so a field the
question does not need is a cost with no benefit. These tests pin the two sides
of that trade: the intent shapes the context, and an unrecognised question keeps
the full context rather than being answered with less than before.
"""

from app.agents.scholar.intent import classify, context_fields, current_focus, guidance, requested_scope


def test_intent_rules_read_both_keyboards():
    assert classify("EE 201'in ön koşulu nedir?") == "prerequisites"
    assert classify("EE 201'in on kosulu nedir?") == "prerequisites"
    assert classify("CENG 331 şubeleri ve hocaları") == "sections"
    assert classify("MATH 260 kaç kredi") == "credits"
    assert classify("planımdaki dersleri saatleriyle göster") == "schedule"
    assert classify("bu dönem kayıt olabilir miyim, kısıt var mı") == "eligibility"
    assert classify("yeni duyurular") == "announcements"
    assert classify("hocama mail at") == "mail"
    assert classify("bunu hatırla: MATLAB sevmiyorum") == "memory"
    assert classify("yönetmelikte nedir bu kural") == "knowledge"


def test_a_programming_question_is_not_a_scheduling_question():
    assert classify("Python programlama nedir") == "knowledge"
    assert classify("merhaba") == "greeting"


def test_a_word_that_merely_contains_a_needle_does_not_fire_it():
    # "açılan" contains "ilan"; the question is about prerequisites, not duyurular.
    assert classify("EE201 dersinin ön koşulu nedir? Açılan şubeleri de yaz.") == "prerequisites"
    assert classify("Açılan şubeler hangileri?") == "sections"


def test_unknown_questions_keep_the_whole_context():
    assert classify("") == "other"
    assert classify("CENG 232 hakkında bir şey söyle") == "other"
    assert context_fields("other") is None


def test_context_follows_the_intent():
    schedule = context_fields("schedule")
    assert "planned_timetable" in schedule
    assert "enabled_tools" in schedule
    assert "academic_term_hint" in schedule
    mail = context_fields("mail")
    assert "enabled_tools" in mail
    assert "planned_timetable" not in mail
    knowledge = context_fields("knowledge")
    assert "planned_timetable" not in knowledge
    assert "local_datetime" not in knowledge
    assert "academic_term_hint" not in knowledge
    for fields in (schedule, mail, knowledge):
        assert {"display_name", "academic_identity", "explicit_memories"} <= fields


def test_guidance_and_focus_are_deterministic():
    assert guidance("prerequisites") and "course_label" in guidance("prerequisites")
    assert guidance("other") is None
    assert current_focus("CENG331'in ön koşulu var mı") == {"courses": ["CENG 331"]}
    assert current_focus("5710331 dersini alabilir miyim") == {"courses": ["5710331"]}
    assert current_focus("EE 201 mi MATH 260 mı") == {"courses": ["EE 201", "MATH 260"]}
    assert current_focus("merhaba") is None


def test_requested_scope_reads_the_term_and_section_a_message_names():
    assert requested_scope("20252'de EE 201'in ön koşulu ne?")["term"] == "20252"
    assert requested_scope("EE 201 şube 2 uygun mu")["section"] == "2"
    assert requested_scope("EE 201 2. şubesi uygun mu")["section"] == "2"
    assert requested_scope("EE 201 section-3 uygun mu")["section"] == "3"
    assert requested_scope("bu dönem EE 201 kaç kredi")["term_unresolved"] is False


def test_a_term_named_in_words_is_reported_unresolved():
    assert requested_scope("2026 güz EE 201 ön koşulu")["term_unresolved"] is True
    assert requested_scope("gelecek dönem EE 201 açılır mı")["term_unresolved"] is True


def test_a_course_number_is_not_read_as_a_section():
    scope = requested_scope("EE 201 şubesi uygun mu")
    assert scope["section"] is None
    assert scope["section_unresolved"] is True
