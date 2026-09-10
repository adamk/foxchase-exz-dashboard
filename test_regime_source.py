from pathlib import Path


APP = Path(__file__).with_name("app.js")


def test_browser_uses_authoritative_server_regime_without_client_reclassification():
    source = APP.read_text(encoding="utf-8")
    assert "confirmedFiveMinutePml" not in source
    assert "correctedPml" not in source
    assert "const normalizedLevels=returnedLevels?{...returnedLevels}:null;" in source
    assert "const displayedRegime=result.regime||'UNKNOWN';" in source
    assert "classifyReturnedLevels" not in source


def test_representative_regimes_are_displayed_from_server_result():
    source = APP.read_text(encoding="utf-8")
    # R1-R6 are server-produced values; the browser must preserve each exactly
    # rather than deriving a second classification from level fields.
    assignment = "const displayedRegime=result.regime||'UNKNOWN';"
    assert assignment in source
    prefix, suffix = source.split(assignment, 1)
    assert "if(pml<ydl" not in prefix
    assert "return 'R6'" not in prefix
    assert "cachedRegimes[date]=displayedRegime" in suffix
