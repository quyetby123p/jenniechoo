import logging
from pathlib import Path

from app.media_research_service import MediaResearchService, _EngineCall
from app.media_settings import MediaSettings


def _dummy_settings(tmp_path: Path) -> MediaSettings:
    return MediaSettings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage" / "media_research",
        logs_root=tmp_path / "logs" / "media_bot",
        state_root=tmp_path / "state" / "media_bot",
        telegram_bot_token="token",
        telegram_allowed_user_id=1,
        daily_run_cap=30,
        timezone_name="Asia/Ho_Chi_Minh",
        serpapi_api_key="serpapi",
        max_image_results=2,
        max_video_results=1,
        max_api_calls_per_run=5,
        platform_allowlist=["facebook.com", "tiktok.com", "youtube.com"],
        market_scope="VN+TH+GLOBAL",
        cloudinary_cloud_name="cloud",
        cloudinary_upload_preset="preset",
        sheet_enabled=True,
        sheet_mode="oauth_user",
        sheet_spreadsheet_id="sheet_1",
        sheet_gid=844064194,
        sheet_oauth_client_id="cid",
        sheet_oauth_client_secret="csecret",
        sheet_oauth_refresh_token="rtoken",
        sheet_oauth_token_uri="https://oauth2.googleapis.com/token",
    )


def test_prepare_candidates_dedupe_and_limits(tmp_path: Path) -> None:
    service = MediaResearchService(_dummy_settings(tmp_path), logging.getLogger("test_media_research"))
    rows = [
        {
            "engine": "google_images",
            "engine_query": "sku",
            "source_url": "https://facebook.com/post/1?utm_source=x",
            "direct_media_url": "https://cdn.fb.com/image1.jpg",
            "thumbnail_url": "",
            "title": "Image 1",
            "snippet": "s",
            "score": 90,
        },
        {
            "engine": "google_images",
            "engine_query": "sku",
            "source_url": "https://facebook.com/post/1?utm_source=y",
            "direct_media_url": "https://cdn.fb.com/image1.jpg",
            "thumbnail_url": "",
            "title": "Image 1 dup",
            "snippet": "s",
            "score": 50,
        },
        {
            "engine": "google_images",
            "engine_query": "sku",
            "source_url": "https://tiktok.com/@a/video/123",
            "direct_media_url": "",
            "thumbnail_url": "",
            "title": "Video",
            "snippet": "s",
            "score": 80,
        },
        {
            "engine": "youtube",
            "engine_query": "sku",
            "source_url": "https://www.youtube.com/watch?v=abc",
            "direct_media_url": "",
            "thumbnail_url": "",
            "title": "YT",
            "snippet": "s",
            "score": 75,
        },
        {
            "engine": "google_images",
            "engine_query": "sku",
            "source_url": "https://example.com/not-allowed",
            "direct_media_url": "",
            "thumbnail_url": "",
            "title": "Ignore",
            "snippet": "s",
            "score": 99,
        },
    ]

    selected = service._prepare_candidates(
        raw_candidates=rows,
        product_code="SKU123",
        query_text="SKU123 fashion",
        created_at="2026-05-19T00:00:00Z",
    )

    assert len(selected) == 2
    assert len([row for row in selected if row["media_type"] == "image"]) == 1
    assert len([row for row in selected if row["media_type"] == "video"]) == 1
    assert all(row["platform"] in {"facebook.com", "tiktok.com", "youtube.com"} for row in selected)


def test_run_research_cloudinary_failure(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaResearchService(_dummy_settings(tmp_path), logging.getLogger("test_media_research_cloudinary"))

    def _raise(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("upload failed")

    monkeypatch.setattr(service, "_upload_to_cloudinary", _raise)

    report = service.run_research(
        run_id="run_1",
        product_code="SKU1",
        keyword_text="",
        photo_bytes=b"abc",
        photo_filename="x.jpg",
    )

    assert report["ok"] is False
    assert report["items"] == []
    assert "upload failed" in report["errors"][0]


def test_run_research_partial_when_engine_error(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaResearchService(_dummy_settings(tmp_path), logging.getLogger("test_media_research_partial"))

    monkeypatch.setattr(service, "_upload_to_cloudinary", lambda *_args, **_kwargs: "https://res.cloudinary.com/x.jpg")
    monkeypatch.setattr(
        service,
        "_engine_calls_primary",
        lambda **_kwargs: [_EngineCall(engine="google_lens", params={"url": "https://x"})],
    )
    monkeypatch.setattr(service, "_should_run_secondary_search", lambda **_kwargs: False)

    def _raise_call(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("serpapi down")

    monkeypatch.setattr(service, "_call_serpapi", _raise_call)

    report = service.run_research(
        run_id="run_2",
        product_code="SKU2",
        keyword_text="",
        photo_bytes=b"abc",
        photo_filename="x.jpg",
    )

    assert report["ok"] is True
    assert report["partial"] is True
    assert report["items"] == []
    assert any("serpapi down" in item for item in report["warnings"])


def test_run_research_skips_secondary_on_image_only_when_lens_has_enough_results(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaResearchService(_dummy_settings(tmp_path), logging.getLogger("test_media_research_skip_secondary"))

    monkeypatch.setattr(service, "_upload_to_cloudinary", lambda *_args, **_kwargs: "https://res.cloudinary.com/x.jpg")
    monkeypatch.setattr(
        service,
        "_engine_calls_primary",
        lambda **_kwargs: [_EngineCall(engine="google_lens", params={"url": "https://x", "type": "visual_matches"})],
    )

    called_secondary = {"value": False}

    def _secondary_calls(**_kwargs):  # noqa: ANN001
        called_secondary["value"] = True
        return []

    monkeypatch.setattr(service, "_engine_calls_secondary", _secondary_calls)

    payload = {
        "visual_matches": [
            {
                "link": f"https://facebook.com/post/{idx}",
                "image": f"https://cdn.fb.com/{idx}.jpg",
                "title": f"samplephoto modelshot {idx}",
                "snippet": "catalog ref",
            }
            for idx in range(10)
        ]
    }
    monkeypatch.setattr(service, "_call_serpapi", lambda *_args, **_kwargs: payload)

    report = service.run_research(
        run_id="run_image_only",
        product_code="AUTO20260519150000",
        keyword_text="",
        photo_bytes=b"abc",
        photo_filename="x.jpg",
    )

    assert report["ok"] is True
    assert called_secondary["value"] is False


def test_run_research_uses_secondary_on_image_only_when_inferred_query_is_strong(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaResearchService(_dummy_settings(tmp_path), logging.getLogger("test_media_research_use_secondary"))

    monkeypatch.setattr(service, "_upload_to_cloudinary", lambda *_args, **_kwargs: "https://res.cloudinary.com/x.jpg")
    monkeypatch.setattr(
        service,
        "_engine_calls_primary",
        lambda **_kwargs: [_EngineCall(engine="google_lens", params={"url": "https://x", "type": "visual_matches"})],
    )

    called_secondary = {"value": False}

    def _secondary_calls(**_kwargs):  # noqa: ANN001
        called_secondary["value"] = True
        return []

    monkeypatch.setattr(service, "_engine_calls_secondary", _secondary_calls)

    payload = {
        "visual_matches": [
            {
                "link": f"https://facebook.com/post/{idx}",
                "image": f"https://cdn.fb.com/{idx}.jpg",
                "title": f"silk sleepwear nightgown {idx}",
                "snippet": "green satin",
            }
            for idx in range(10)
        ]
    }
    monkeypatch.setattr(service, "_call_serpapi", lambda *_args, **_kwargs: payload)

    report = service.run_research(
        run_id="run_image_only_secondary",
        product_code="AUTO20260519150001",
        keyword_text="",
        photo_bytes=b"abc",
        photo_filename="x.jpg",
    )

    assert report["ok"] is True
    assert called_secondary["value"] is True
