"""response_text skips thinking blocks (extract crash, 2026-10-18)."""

from types import SimpleNamespace

from cognify._anthropic_auth import response_text


def _block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def test_skips_leading_thinking_block():
    resp = SimpleNamespace(content=[
        _block("thinking", thinking="hmm", signature="x"),
        _block("text", text='{"lessons": []}'),
    ])
    assert response_text(resp) == '{"lessons": []}'


def test_joins_text_blocks_and_handles_empty():
    resp = SimpleNamespace(content=[_block("text", text="YES"), _block("text", text=" ok")])
    assert response_text(resp) == "YES ok"
    assert response_text(SimpleNamespace(content=[])) == ""
