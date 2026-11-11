from types import SimpleNamespace

from app.dropo_pancake_bridge import DropoPancakeBridge
from scripts.repair_jennie_order_variation import _item_code, _replace_item_variation


def _bridge_stub():
    return SimpleNamespace(_normalize_jennie_code=DropoPancakeBridge._normalize_jennie_code)


def test_item_code_reads_descriptive_variation_info_sku():
    item = {
        "variation_info": {
            "custom_id": "JC-A-250-BE: AO REN MAU BE-S",
            "name": "Falling Cloud JCA250",
        }
    }

    assert _item_code(_bridge_stub(), item) == "JC-A-250"


def test_item_code_reads_nested_variation_values():
    item = {
        "variation_values": [
            {"value": "JC-A-260-AO KEM-S"},
            {"value": "Soya Tea-shirt JCA260"},
        ]
    }

    assert _item_code(_bridge_stub(), item) == "JC-A-260"


def test_item_code_handles_typographic_hyphens():
    item = {
        "variation_info": {
            "custom_id": "JC‑A‑250‑BE: AO REN MAU BE-S",
        }
    }

    assert _item_code(_bridge_stub(), item) == "JC-A-250"


def test_choose_variation_prefers_exact_black_token_over_cream_description():
    candidates = [
        {
            "variation_id": "cream-s",
            "variation_sku": "JC-A-250-BE: AO REN MAU BE-S",
            "size": "S",
            "field_values": ["JC-A-250-BE: AO REN MAU BE-S"],
        },
        {
            "variation_id": "black-s",
            "variation_sku": "JC-A-250-DEN: AO DEN-S",
            "size": "S",
            "field_values": ["JC-A-250-DEN: AO DEN-S"],
        },
    ]

    chosen = DropoPancakeBridge._choose_jennie_variation(
        candidates,
        color="Đen",
        size="S",
    )

    assert chosen["variation_id"] == "black-s"


def test_replace_variation_always_writes_item_level_variation_id():
    black = {
        "variation_id": "black-s",
        "variation_sku": "JC-A-250-DEN: AO DEN-S",
        "size": "S",
        "field_values": ["JC-A-250-DEN: AO DEN-S"],
    }
    bridge = SimpleNamespace(
        _normalize_jennie_code=DropoPancakeBridge._normalize_jennie_code,
        _choose_jennie_variation=DropoPancakeBridge._choose_jennie_variation,
        _pancake_catalog={"JC-A-250": [black]},
    )
    order = {
        "items": [
            {
                "variation_info": {
                    "custom_id": "JC-A-250-BE: AO REN MAU BE-S",
                }
            }
        ]
    }

    updated, change = _replace_item_variation(
        bridge,
        order,
        code="JC-A-250",
        color="Đen",
        size="S",
        occurrence=1,
    )

    assert change["variation_id"] == "black-s"
    assert updated["items"][0]["variation_id"] == "black-s"
