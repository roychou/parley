"""Tests for the EDGAR filing-document layer (clean_text + extract_sections).

These use synthetic, well-formed filings to prove the extraction *algorithm*
(TOC-vs-body disambiguation via largest span, validation gate). Real-filer
precision is a separate tuning problem (see notes/sentiment-specialist-design.md);
on filings where extraction can't confidently locate a section it returns None and
the caller falls back to the whole-filing map-reduce path.
"""
from src.data.edgar_filings import clean_text, extract_sections

_MDNA_BODY = "<p>Revenue increased 18% driven by cloud growth. </p>" * 120
_RISK_BODY = "<p>Our business could be adversely affected by competition. </p>" * 120

# A well-formed 10-Q: TOC (dense, short spans) then body sections (long spans),
# with HTML tags and entities to exercise clean_text.
_TENQ_HTML = (
    "<html><body>"
    "<table><tr><td>Item 2. Management&#8217;s Discussion and Analysis</td><td>5</td></tr>"
    "<tr><td>Item 3. Quantitative and Qualitative Disclosures</td><td>20</td></tr>"
    "<tr><td>Item 1A. Risk Factors</td><td>40</td></tr>"
    "<tr><td>Item 2. Unregistered Sales of Equity Securities</td><td>45</td></tr></table>"
    "<h2>Item 2. Management&#8217;s Discussion and Analysis of Financial Condition "
    "and Results of Operations</h2>" + _MDNA_BODY +
    "<h2>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h2>"
    "<p>Not materially changed.</p>"
    "<h2>Item 1A. Risk Factors</h2>" + _RISK_BODY +
    "<h2>Item 2. Unregistered Sales of Equity Securities and Use of Proceeds</h2>"
    "<p>None.</p>"
    "</body></html>"
)


def test_clean_text_strips_tags_and_decodes_entities():
    out = clean_text("<p>Management&#8217;s&nbsp;Discussion</p>")
    assert "<" not in out
    assert "Management’s" in out  # entity decoded
    assert "  " not in out  # whitespace collapsed


def test_extract_mdna_skips_toc_takes_body():
    secs = extract_sections(clean_text(_TENQ_HTML), "10-Q")
    assert secs["mdna"] is not None
    # Body, not the one-line TOC entry.
    assert "Results of Operations" in secs["mdna"]
    assert "Revenue increased" in secs["mdna"]
    # Bounded at the next section — doesn't run into Risk Factors content.
    assert "adversely affected" not in secs["mdna"]


def test_extract_risk_factors_body():
    secs = extract_sections(clean_text(_TENQ_HTML), "10-Q")
    assert secs["risk_factors"] is not None
    assert "Risk Factors" in secs["risk_factors"]
    assert "adversely affected" in secs["risk_factors"]


def test_unknown_form_returns_none():
    secs = extract_sections(clean_text(_TENQ_HTML), "8-K")
    assert secs == {"mdna": None, "risk_factors": None}


def test_amended_form_maps_to_base():
    # 10-Q/A should use the 10-Q spec.
    secs = extract_sections(clean_text(_TENQ_HTML), "10-Q/A")
    assert secs["mdna"] is not None


def test_garbage_section_falls_back_to_none():
    # No recognizable headers -> nothing extracted (caller falls back).
    secs = extract_sections(clean_text("<p>" + ("blah " * 5000) + "</p>"), "10-Q")
    assert secs == {"mdna": None, "risk_factors": None}
