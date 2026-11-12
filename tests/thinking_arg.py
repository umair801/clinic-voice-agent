"""The thinking field must suit each model. Sonnet 5.5 rejects {"type": "disabled"} (400), Haiku 5.5 accepts it."""
def main():
    from ava.voice.agent import thinking_arg
    assert thinking_arg("claude-sonnet-5-5", "disabled") == {"type": "between_tools"}
    assert thinking_arg("claude-haiku-5-5", "disabled") == {"type": "disabled"}
    assert thinking_arg("claude-sonnet-5-5", "default") is None
    assert thinking_arg("claude-haiku-5-5", "default") is None
    print("thinking_arg ok")
