from app.planning.catalog_service import constraint_rows


def test_constraint_extraction_keeps_list_rows_and_rejects_unknown_shapes():
    rows = [{"given_dept": "CENG", "start_char": "AA", "end_char": "ZZ"}]
    assert constraint_rows(rows) == rows
    assert constraint_rows({"constraints": rows}) == rows
    assert constraint_rows({"constraints": {"given_dept": "CENG"}}) is None
    assert constraint_rows({"constraints": [{"unrelated": "value"}]}) is None


def test_empty_constraint_table_is_verified_open():
    assert constraint_rows([]) == []
    assert constraint_rows({"constraints": []}) == []
