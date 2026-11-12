"""Training by word: the player reporting back, and a fellow guard agreeing to the fist bout."""
import pytest

from livingworld.guard import training_words as TW


@pytest.mark.parametrize("text", ["Reporting back, sir.", "Commander, reporting in.", "I'm back. The drill is done.",
                                  "I've done the drill.", "The bout's over.", "Here to report, Commander.",
                                  "I have finished my duty."])
def test_reporting_back(text):
    assert TW.says_reporting(text)


@pytest.mark.parametrize("text", ["When should I report back?", "I haven't done the drill yet.",
                                  "Where do I report?", "Hello Caius.", "I'm not done yet."])
def test_not_reporting_back(text):
    assert not TW.says_reporting(text)


@pytest.mark.parametrize("text", ["Put up your fists, recruit!", "Alright, let's spar.", "Fists up, then.",
                                  "You're on. Show me what you've got!", "Very well, I'll go a round with you."])
def test_agrees_to_spar(text):
    assert TW.says_spar(text)


@pytest.mark.parametrize("text", ["Want to spar?", "Not now, I'm on watch.", "Maybe later we can spar.",
                                  "Let's fight with steel.", "Put away your fists."])
def test_does_not_agree_to_spar(text):
    assert not TW.says_spar(text)


def test_spar_watcher_adds_brawl_once_for_a_told_guard_only():
    w = TW.SparWatcher({"Whiterun Guard"})
    acts: list = []
    assert w.keep("Ysolda", "Put up your fists!", acts) == [] and acts == []
    assert w.keep("Whiterun Guard", "Put up your fists!", acts) == [TW.BRAWL]
    assert acts == [{"identifier": TW.BRAWL, "arguments": {"source": ["Whiterun Guard"]}}]
    assert w.keep("Whiterun Guard", "Let's spar.", []) == []
