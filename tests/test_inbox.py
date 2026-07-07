from nrfm.inbox import apply_commands, normalize_ticker, parse_commands
from nrfm.store import Store


def test_parse_commands_variants():
    body = """
    Hi, please update:
    add SOBI
    ADD eric-b
    remove ABB
    rm SAND
    equity 150 000
    list
    > add QUOTED.ST   (this is a reply quote, ignored)
    some unrelated sentence
    """
    cmds = parse_commands(body)
    assert cmds == [("add", "SOBI"), ("add", "eric-b"), ("rm", "ABB"),
                    ("rm", "SAND"), ("equity", "150 000"), ("list", "")]


def test_normalize_ticker():
    assert normalize_ticker("sobi") == "SOBI.ST"
    assert normalize_ticker("ERIC B") == "ERIC-B.ST"
    assert normalize_ticker("SSAB-B.ST") == "SSAB-B.ST"


def test_apply_commands_validates_against_universe(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.upsert_instruments([
        {"orderbook_id": "TX1", "symbol": "SOBI", "isin": "SE1",
         "name": "Sobi", "currency": "SEK", "segment": "LARGE_CAP",
         "sector": "Health", "yahoo_ticker": "SOBI.ST"},
    ], seen_date="2026-07-01")

    results = apply_commands(store, [
        ("add", "SOBI"),
        ("add", "NOTREAL"),       # not in universe -> rejected
        ("rm", "SOBI"),
        ("rm", "SOBI"),           # already removed -> rejected
        ("equity", "150 000"),
        ("equity", "abc"),        # bad number -> rejected
    ])
    outcomes = [r.outcome for r in results]
    assert "holding added: SOBI.ST" in outcomes[0]
    assert outcomes[1].startswith("REJECTED")
    assert "holding removed: SOBI.ST" in outcomes[2]
    assert outcomes[3].startswith("REJECTED")
    assert "150,000 SEK" in outcomes[4]
    assert outcomes[5].startswith("REJECTED")
    assert store.holdings() == []
    assert store.state_get("portfolio_equity_sek") == "150000.0"
    store.close()
