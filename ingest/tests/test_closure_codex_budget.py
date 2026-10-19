"""The codex transcript budget must fit codex exec's input cap."""

import json

import close_orphan_sessions as m


def test_codex_prompt_fits_input_cap():
    header = m.PROMPT_HEADER.format(
        schema=json.dumps(m.PAYLOAD_SCHEMA), breadcrumbs="(none recorded)", tail="")
    # extract_*_text may add an elision marker on top of the budget.
    assert len(header) + m.BUDGETS["codex"] + 200 < m.CODEX_MAX_INPUT_CHARS
