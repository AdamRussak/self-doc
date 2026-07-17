from app.extract import extract


def test_extracts_main_content_as_markdown():
    html = """
    <html><body>
    <nav>Skip this nav content entirely please</nav>
    <article>
    <h1>Getting Started</h1>
    <p>This is a fairly long paragraph of real documentation content that should
    be extracted successfully by trafilatura because it exceeds the minimum
    length threshold required for a page to be considered valid extraction
    output rather than junk or empty boilerplate text.</p>
    <p>Here is a second paragraph with even more useful detail about how to use
    this fictional library, including examples and explanations that a reader
    would find helpful when learning the API for the first time.</p>
    </article>
    <footer>Skip this footer content entirely please</footer>
    </body></html>
    """
    result = extract("https://example.com/getting-started", html)
    assert result.status == "ok"
    assert result.markdown is not None
    assert len(result.markdown) >= 200
    assert "nav" not in result.markdown.lower() or "Getting Started" in result.markdown


def test_rejects_too_short_extraction():
    html = "<html><body><p>Too short.</p></body></html>"
    result = extract("https://example.com/empty", html)
    assert result.status == "skipped"
    assert result.markdown is None
    assert result.reason
