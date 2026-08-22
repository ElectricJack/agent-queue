from src.database.tables import messages


def test_pane_open_columns_exist():
    col_names = {c.name for c in messages.columns}
    assert "body_kind" in col_names
    assert "pane_open" in col_names
