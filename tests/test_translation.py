from handle_mcp import translation


def test_english_translation_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("HANDLE_LANGUAGE", raising=False)
    assert translation.enabled() is True


def test_danish_output_can_be_selected(monkeypatch):
    monkeypatch.setenv("HANDLE_LANGUAGE", "da")
    assert translation.enabled() is False


def test_payload_translates_display_text_but_preserves_identifiers(monkeypatch):
    monkeypatch.setenv("HANDLE_LANGUAGE", "en")
    monkeypatch.setattr(translation, "translate_text", lambda text: f"EN:{text}")

    payload = {
        "products": [
            {
                "id": "12345",
                "ean": "5701234567890",
                "name": "Hakket oksekød",
                "brand": "Dansk Brand",
                "category": "Kød > Oksekød",
                "price": "29.95",
                "size": "400 g",
                "url": "https://example.com/product/12345",
                "offer_text": "2 for 40 kr.",
            }
        ],
        "fees": {"levering": "39.00"},
        "recipe": {
            "name": "Kylling i karry",
            "ingredients": ["2 løg", "400 g kylling"],
            "method": [{"title": "Tilberedning", "steps": ["Skær løgene."]}],
        },
    }

    result = translation.translate_payload(payload)

    product = result["products"][0]
    assert product["name"] == "EN:Hakket oksekød"
    assert product["category"] == "EN:Kød > Oksekød"
    assert product["offer_text"] == "EN:2 for 40 kr."
    assert product["id"] == "12345"
    assert product["ean"] == "5701234567890"
    assert product["brand"] == "Dansk Brand"
    assert product["price"] == "29.95"
    assert product["size"] == "400 g"
    assert product["url"] == "https://example.com/product/12345"
    assert result["fees"] == {"EN:levering": "39.00"}
    assert result["recipe"]["name"] == "EN:Kylling i karry"
    assert result["recipe"]["ingredients"] == ["EN:2 løg", "EN:400 g kylling"]
    assert result["recipe"]["method"][0]["steps"] == ["EN:Skær løgene."]


def test_disabled_translation_returns_original_payload(monkeypatch):
    monkeypatch.setenv("HANDLE_LANGUAGE", "da")
    payload = {"name": "Hakket oksekød", "id": "123"}
    assert translation.translate_payload(payload) is payload
