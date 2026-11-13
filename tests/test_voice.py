"""Voice mode (Nova Sonic) helpers: no AWS calls."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "web"), str(ROOT / "agents")]

import voice  # noqa: E402


def test_voice_prompt_asks_name_and_routes_to_tools():
    p = voice.voice_prompt(None, {})
    assert "how you may address them" in p and "travel_buddy" in p and "ui_action" in p
    assert "Pack your bags and follow with Abhi" in p and "ASEAN" in p


def test_voice_prompt_uses_name_and_interests():
    p = voice.voice_prompt("Maya", {"interests": ["beaches", "street food"], "budget": "budget", "dislikes": []})
    assert "name is Maya" in p and "beaches, street food" in p and "budget" in p


def test_ui_actions_are_a_fixed_safe_set():
    assert set(voice.UI_ACTIONS) == {"show", "scroll", "play_video", "open_chart", "click_suggestion", "new_chat", "open_status"}
    assert voice.MODEL_ID == "amazon.nova-2-5-sonic" and voice.MAX_CONCURRENT >= 1 and voice.MAX_SECONDS <= 8 * 60


def test_speech_guard_mutes_action_narration():
    for said in ("Ooh, let me look that up!", "One moment please.", "I'll scroll to the hotels for you.",
                 "Pulling that up now!", "I'm going to check flights.", "Checking the latest prices…", "Hang on!"):
        assert voice.is_narration(said), said
    for answer in ("Nusa Dua Beach is a calm, sandy gem with 4.7 stars.", "Hi Maya, lovely to meet you!",
                   "Bangkok has amazing temples like Wat Arun.", "Flights start at 120 dollars."):
        assert not voice.is_narration(answer), answer


def test_strip_next_actions_keeps_only_the_answer():
    reply = "Top pick: Nusa Dua Beach (4.7⭐). Want me to find hotels nearby? Padang Padang is great for surfing. Shall I check flights too?"
    out = voice.strip_next_actions(reply)
    assert "Nusa Dua" in out and "Padang Padang" in out
    assert "Want me" not in out and "Shall I" not in out
    assert voice.strip_next_actions("Would you like me to book it?") == ""


def test_prompt_states_the_rule():
    p = voice.voice_prompt("Maya", {})
    assert "SPEAK ONLY THE ANSWER" in p and "let me look that up" not in p.replace("no \"let me check\"", "")
