from __future__ import annotations

import logging

from app.meta_ads_client import MetaAdsClient


class RollbackService:
    def __init__(self, meta_client: MetaAdsClient, logger: logging.Logger) -> None:
        self.meta_client = meta_client
        self.logger = logger

    def rollback(
        self,
        campaign_id: str | None,
        adset_ids: list[str],
        ad_ids: list[str],
        creative_ids: list[str] | None = None,
    ) -> None:
        if not campaign_id and not adset_ids and not ad_ids and not (creative_ids or []):
            return
        self.logger.warning(
            "Bat dau rollback: campaign=%s adsets=%s ads=%s creatives=%s",
            campaign_id,
            adset_ids,
            ad_ids,
            creative_ids or [],
        )
        self.meta_client.rollback_tree(campaign_id, adset_ids, ad_ids, creative_ids)
