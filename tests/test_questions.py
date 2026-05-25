from claude_dingtalk_bridge.questions import format_question, parse_answer

SINGLE = {
    "question": "Which database should we use?",
    "header": "Database",
    "multiSelect": False,
    "options": [
        {"label": "Postgres", "description": "Relational, robust"},
        {"label": "SQLite", "description": "Embedded, zero-config"},
    ],
}

MULTI = {
    "question": "Which features to enable?",
    "header": "Features",
    "multiSelect": True,
    "options": [
        {"label": "Auth", "description": "Login"},
        {"label": "Billing", "description": "Payments"},
    ],
}


def test_format_question_single():
    msg = format_question(SINGLE, index=0, total=1)
    assert "❓ Claude is asking" in msg
    assert "(1/1)" not in msg  # no counter for a lone question
    assert "▌ Database" in msg
    assert "Which database should we use?" in msg
    assert "1. Postgres" in msg
    assert "   Relational, robust" in msg
    assert "2. SQLite" in msg
    assert "Reply with a number, or type your own answer." in msg


def test_format_question_counter_for_multiple():
    msg = format_question(SINGLE, index=1, total=3)
    assert "(2/3)" in msg


def test_format_question_multiselect_hint():
    msg = format_question(MULTI, index=0, total=1)
    assert "Reply with numbers (e.g. 1,3), or type your own answer." in msg


def test_parse_answer_number_maps_to_label():
    answer, valid = parse_answer("1", SINGLE["options"])
    assert valid is True
    assert answer == "Postgres"


def test_parse_answer_multiple_numbers():
    answer, valid = parse_answer("1,2", MULTI["options"])
    assert valid is True
    assert answer == "Auth, Billing"


def test_parse_answer_free_text():
    answer, valid = parse_answer("use DynamoDB instead", SINGLE["options"])
    assert valid is True
    assert answer == "use DynamoDB instead"


def test_parse_answer_out_of_range_is_invalid():
    answer, valid = parse_answer("9", SINGLE["options"])
    assert valid is False
    assert answer is None


def test_format_question_omits_blank_header_and_text():
    # An entry with empty header and empty question renders neither line,
    # but still lists its options.
    rendered = format_question(
        {"header": "", "question": "", "options": [{"label": "Yes"}]}, 0, 1
    )
    assert "▌" not in rendered
    assert "1. Yes" in rendered
