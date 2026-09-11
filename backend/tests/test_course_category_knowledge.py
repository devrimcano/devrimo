"""What METU's category names mean, stated once where the model reads it.

"kanka free elective alcam ne alim" took twelve tool calls and two and a half
minutes and answered in topic headings - "Python/C ve veri yapıları", "olasılık
temelli ders" - without a single course code. Asked again for codes, it gave
the same headings again.

The data was reachable the whole time. Read against the live source:

    student.categories            -> FREE ELECTIVE is category id 5-567
    category_courses "7-567"      -> 174 real courses, codes and names
    category_courses "5-567"      -> 0 courses, and a message field saying
                                     "You can choose any course in any department"

So FREE ELECTIVE genuinely has no list - that is what free means - and the
source says so in a field the assistant never read. It had no way to tell that
apart from a failure, so it hedged.

These tests pin the instruction, because it is the difference between an answer
and a hedge on the question this product exists to answer.
"""

from app.agents.scholar.prompt import build_instructions


def _text() -> str:
    return "\n".join(build_instructions())


def test_every_category_the_source_returns_is_explained():
    """All five appear in a real student's student.categories payload."""
    instructions = _text()
    for category in (
        "MUST COURSE",
        "TECHNICAL ELECTIVE",
        "NONTECHNICAL ELECTIVE",
        "RESTRICTED ELECTIVE",
        "FREE ELECTIVE",
    ):
        assert category in instructions, f"{category} is unexplained"


def test_free_elective_is_named_as_having_no_list():
    """The specific fact that turned a good answer into a hedge.

    An empty result for FREE ELECTIVE is correct, not broken, and the model has
    to know that before it can say so instead of retrying.
    """
    instructions = _text().casefold()
    assert "no list to fetch" in instructions
    assert "not a failure" in instructions


def test_the_model_is_told_to_read_the_message_field_on_an_empty_result():
    """The source explains its own empty answers; that explanation is the answer."""
    assert "message field" in _text()


def test_categories_are_read_by_id_rather_than_by_name():
    """Reading the category by its name returns nothing.

    Verified against the live source: key "FREE ELECTIVE" answers with
    "Please Choose Program Category" and no courses, while key "5-567" answers
    properly. The ids come from student.categories and are per student.
    """
    instructions = _text()
    assert "student.categories" in instructions
    assert "id" in instructions
