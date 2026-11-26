"""Locked defaults for the direct-media ad flows."""

LOCKED_MEDIA_HEADLINE = "Free shipping on all orders. 7-day returns from the date of delivery."
LOCKED_WEB_CTA_LABEL = "Mua ngay"
LOCKED_WEB_CTA_TYPE = "SHOP_NOW"
LOCKED_WEB_DESTINATION_URL = "https://th.jcdejc.com/"


def locked_web_fields() -> dict[str, str]:
    """Return the fields that must not be user-editable in the Web media flow."""

    return {
        "headline": LOCKED_MEDIA_HEADLINE,
        "cta_label": LOCKED_WEB_CTA_LABEL,
        "cta_type": LOCKED_WEB_CTA_TYPE,
        "destination_url": LOCKED_WEB_DESTINATION_URL,
    }

