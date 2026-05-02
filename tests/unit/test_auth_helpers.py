import hashlib

from lumen.blueprints.auth.routes import gravatar_md5, make_initials


def test_gravatar_md5_known():
    expected = hashlib.md5("test@example.com".encode()).hexdigest()
    assert gravatar_md5("test@example.com") == expected


def test_gravatar_md5_strips_whitespace():
    assert gravatar_md5("  test@example.com  ") == gravatar_md5("test@example.com")


def test_gravatar_md5_case_insensitive():
    assert gravatar_md5("Test@Example.COM") == gravatar_md5("test@example.com")


def test_make_initials_two_words():
    assert make_initials("John Doe") == "JD"


def test_make_initials_first_last():
    assert make_initials("Alice Bob") == "AB"


def test_make_initials_three_words_uses_first_last():
    assert make_initials("John Middle Doe") == "JD"


def test_make_initials_one_word():
    assert make_initials("Alice") == "AL"


def test_make_initials_one_short_word():
    assert make_initials("A") == "A"


def test_make_initials_empty():
    assert make_initials("") == "??"


def test_make_initials_whitespace_only():
    assert make_initials("  ") == "??"


def test_make_initials_uppercase():
    assert make_initials("john doe") == "JD"
