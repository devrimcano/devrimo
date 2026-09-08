from app.campus.curriculum import all_semester_courses
from app.api.v1.schedule import CourseSectionsResponse, _published_terms
from app.planning.catalog import normalize_sections


def test_published_terms_accept_real_five_digit_term_codes():
    assert _published_terms({"semesters": [{"semester": "20261"}, {"term_code": "20262"}, {"code": "20263"}]}) == ["20263", "20262", "20261"]


def test_catalog_sections_are_server_normalized_to_exact_minutes():
    sections = normalize_sections({
        "sections": [
            {
                "section": "01",
                "instructors": ["Prof X"],
                "critical_info": "Surname AA-ZZ",
                "schedule": [
                    {"day": "Monday", "time": "08:40-10:30", "room": "P1"},
                    {"day": "Wed", "time": "13:40-14:30", "room": "B07"},
                ],
            }
        ]
    })

    assert sections == [{
        "section": "01",
        "instructor": "Prof X",
        "meetings": [
            {"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "P1"},
            {"day": "Wed", "start_minute": 820, "duration_minutes": 50, "room": "B07"},
        ],
        "constraint": "Surname AA-ZZ",
    }]


def test_catalog_json_text_is_normalized_without_browser_parsing():
    sections = normalize_sections(
        '[{"section": "2", "schedule": "Tuesday 09:40-11:30"}]'
    )

    assert sections[0]["meetings"] == [
        {"day": "Tue", "start_minute": 580, "duration_minutes": 110, "room": ""}
    ]


def test_course_sections_response_preserves_signed_alias_fields():
    response = CourseSectionsResponse.model_validate({
        "data": {},
        "sections": [{
            "section": "1",
            "eligibility_course_code": "MATH119",
            "eligibility_raw_code": "2360119",
        }],
    })

    section = response.model_dump(mode="json")["sections"][0]
    assert section["eligibility_course_code"] == "MATH119"
    assert section["eligibility_raw_code"] == "2360119"


def test_full_curriculum_keeps_completed_failed_and_outstanding_rows():
    rows = all_semester_courses({
        "semesters": [
            {"semester": 2, "completed": True, "courses": [{"course_code": "2300101", "course_name": "Math", "grade": "AA", "credit": 3}]},
            {"semester": 1, "completed": False, "courses": [
                {"course_code": "4330101", "course_name": "Physics", "grade": "FF", "credit": "4"},
                {"course_code": "1230101", "course_name": "Chemistry", "grade": "", "credit": 3},
            ]},
        ]
    })
    assert [(row["course_code"], row["status"]) for row in rows] == [
        ("4330101", "failed"),
        ("1230101", "outstanding"),
        ("2300101", "completed"),
    ]
