"""Context selection has to answer the same questions with less, not less well.

Every dependency field is re-sent on every model step of a run, so a field the
question does not need is a cost with no benefit. These tests pin the two sides
of that trade: the intent shapes the context, and an unrecognised question keeps
the full context rather than being answered with less than before.
"""

from app.agents.scholar.intent import classify, context_fields, current_focus, guidance


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
