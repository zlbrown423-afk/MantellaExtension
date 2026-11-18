from livingworld.audio.prompt_echo import strip_prompt_echo

HINT = "This is a conversation with Hulda in The Bannered Mare."


def test_whole_echo_is_dropped():
    assert strip_prompt_echo(" This is a conversation with Hulda in The Bannered Mare.", HINT) == ""


def test_echo_without_punctuation_or_case():
    assert strip_prompt_echo("this is a conversation with hulda in the bannered mare", HINT) == ""


def test_partial_echo_is_dropped():
    assert strip_prompt_echo("This is a conversation with Hulda.", HINT) == ""
    assert strip_prompt_echo("This is a conversation with Hulda in Whiterun.", HINT) == ""


def test_echo_before_real_speech_keeps_the_speech():
    assert strip_prompt_echo("This is a conversation with Hulda in The Bannered Mare. Can I get a room?", HINT) == "Can I get a room?"


def test_real_speech_untouched():
    for said in ["Hello Hulda, how are you?", "This is a nice inn.", "This is a conversation I didn't want to have."]:
        assert strip_prompt_echo(said, HINT) == said


def test_group_hint():
    hint = "This is a conversation with Hulda and Saadia in The Bannered Mare."
    assert strip_prompt_echo("This is a conversation with Hulda and Saadia in The Bannered Mare.", hint) == ""


def test_off_switch(monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_PROMPT_ECHO", "0")
    assert strip_prompt_echo(HINT, HINT) == HINT


def test_no_hint():
    assert strip_prompt_echo("Hello", "") == "Hello"
