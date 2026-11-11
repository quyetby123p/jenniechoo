from types import SimpleNamespace

from app.dropo_pancake_bridge import DropoPancakeBridge
from scripts.repair_jennie_order_variation import _item_code


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
