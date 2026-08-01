"""Name extraction from noisy Gujarati STT replies."""
from agent.names import extract_name, with_honorific


def test_plain_name():
    assert extract_name("રમેશ") == "રમેશ"


def test_maru_naam_pattern():
    assert extract_name("મારું નામ રમેશ પટેલ છે") == "રમેશ પટેલ"


def test_hu_pattern():
    assert extract_name("હું સુરેશ છું") == "સુરેશ"


def test_english_spillover():
    assert extract_name("my name is Ramesh Patel") == "Ramesh Patel"


def test_hindi_spillover():
    assert extract_name("मेरा नाम सुरेश है") == "सुरेश"


def test_only_filler_returns_none():
    assert extract_name("મારું નામ છે") is None
    assert extract_name("હા જી") is None
    assert extract_name("") is None
    assert extract_name("   ") is None


def test_caps_at_three_words():
    assert extract_name("રમેશ કુમાર પટેલ અમદાવાદ થી") == "રમેશ કુમાર પટેલ"


def test_honorific():
    assert with_honorific("રમેશ") == "રમેશભાઈ"
    assert with_honorific("રમેશ પટેલ") == "રમેશભાઈ"
    assert with_honorific("રમેશભાઈ") == "રમેશભાઈ"
    assert with_honorific("સીતાબેન") == "સીતાબેન"
