from app.services.warehouse_sync import fingerprint, transform


def test_transform():
    row = {"id": "P0000001", "name": "Test Product", "category": "SPICES", "price": "249.50", "currency": "INR", "updated_at": "2026-09-25T08:30:00Z"}
    result = transform(row)
    assert result["sku"] == "P0000001"
    assert result["selling_price"] == 249.50
    assert result["category_code"] == "SPICES"


def test_fingerprint_changes_only_when_the_payload_changes():
    product = {"sku": "P1", "description": "One", "selling_price": 10.0}
    assert fingerprint(product) == fingerprint(dict(reversed(list(product.items()))))
    assert fingerprint(product) != fingerprint({**product, "selling_price": 11.0})
