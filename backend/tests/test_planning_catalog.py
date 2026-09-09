from app.planning.catalog import normalize_sections


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


def test_published_section_code_is_kept_as_the_section_identity():
    sections = normalize_sections({
        "sections": [{
            "section_code": "31",
            "meetings": [{"weekday": 0, "start_minute": 540, "end_minute": 590}],
        }],
    })
    assert sections == [{
        "section": "31",
        "instructor": "",
        "meetings": [{"day": "Mon", "start_minute": 540, "duration_minutes": 50, "room": ""}],
        "constraint": "",
    }]
