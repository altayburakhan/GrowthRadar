from growthradar.collection.extractor import detect_cta_signals, estimate_pricing_tiers, parse_page


def test_parse_page_extracts_title_and_meta():
    html = """
    <html><head><title>Acme Inc</title>
    <meta name="description" content="Project management for teams"></head>
    <body><h1>Welcome</h1><script>var x = 1;</script></body></html>
    """
    title, meta, text = parse_page(html, "https://acme.com")
    assert title == "Acme Inc"
    assert meta == "Project management for teams"
    assert "Welcome" in text
    assert "var x" not in text


def test_detect_cta_signals():
    signals = detect_cta_signals("Start your free trial today, no credit card required.")
    assert signals["has_free_trial_cta"] is True
    assert signals["has_demo_cta"] is False


def test_estimate_pricing_tiers():
    text = "Basic $10/month, Pro $25/month, Enterprise $99/month"
    assert estimate_pricing_tiers(text) >= 2
