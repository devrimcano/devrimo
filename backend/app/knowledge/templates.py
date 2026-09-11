"""Reviewed starter templates. They are installed as drafts, never auto-published."""

DEFAULT_SOURCE_TEMPLATES = (
    {
        "id": "registrar-calendar-2026-2027",
        "name": "ODTÜ 2026-2027 Akademik Takvimi",
        "kind": "html_page",
        "url": "https://oidb.metu.edu.tr/tr/odtu-ankara-ve-erdemli-kampusleri-2026-2027-akademik-takvim",
        "language": "tr",
        "authority": 100,
        "audience": {"academic_year": "2026-2027", "campus": "Ankara ve Erdemli"},
        "schedule_seconds": 86_400,
        "config": {
            "content_selector": "article .field--name-body",
            "title_selector": "article h1, h1",
            "external_id": "oidb-academic-calendar-2026-2027",
            "defaults": {
                "record_type": "calendar",
                "campus": "Ankara ve Erdemli",
                "audience": {"academic_year": "2026-2027"},
            },
        },
    },
    {
        "id": "dorm-announcements",
        "name": "METU Dormitory Announcements",
        "kind": "drupal",
        "url": "https://yurtlar.metu.edu.tr",
        "language": "tr",
        "authority": 95,
        "audience": {},
        "schedule_seconds": 10_800,
        "config": {
            "item_selector": "article, .views-row",
            "defaults": {"record_type": "announcement"},
        },
    },
    {
        "id": "sports-announcements",
        "name": "METU Sports Directorate Announcements",
        "kind": "drupal",
        "url": "https://spormd.metu.edu.tr/tr",
        "language": "tr",
        "authority": 95,
        "audience": {},
        "schedule_seconds": 3600,
        "config": {
            "item_selector": "article, .views-row",
            "defaults": {"record_type": "service_status"},
        },
    },
    {
        "id": "meturoam-guide",
        "name": "METU CC eduroam Guide",
        "kind": "html_page",
        "url": "https://faq.cc.metu.edu.tr/tr/sss/meturoam",
        "language": "tr",
        "authority": 100,
        "audience": {},
        "schedule_seconds": 604_800,
        "config": {
            "content_selector": "main",
            "title_selector": "h1",
            "defaults": {"record_type": "guide"},
        },
    },
)
