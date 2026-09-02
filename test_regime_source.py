from pathlib import Path


APP = Path(__file__).with_name("app.js")


def test_browser_uses_authoritative_returned_levels_without_pml_override():
    source = APP.read_text(encoding="utf-8")
    assert "confirmedFiveMinutePml" not in source
    assert "correctedPml" not in source
    assert "const normalizedLevels=returnedLevels?{...returnedLevels}:null;" in source
    assert "classifyReturnedLevels(normalizedLevels,result.regime)" in source
