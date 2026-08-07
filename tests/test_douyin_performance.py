import json
import tempfile
import unittest
from pathlib import Path

import httpx

from qijia_video.contracts import (
    Actor,
    DouyinPerformance,
    DouyinPlaybackSnapshot,
    JobState,
    ProviderUsageRecord,
    VideoJob,
)
from qijia_video.cost_analysis import build_douyin_performance_analysis
from qijia_video.errors import ProviderUnavailable, QualityGateFailed
from qijia_video.infrastructure.tikhub import (
    TikHubDouyinPerformanceProvider,
    normalize_douyin_share_url,
)
from qijia_video.ports import DouyinVideoPerformance
from qijia_video.service import QijiaVideoService


VIDEO_ID = "7512756548356492544"


class MemoryJobRepository:
    def __init__(self, job: VideoJob):
        self.document = job.model_dump(mode="json")

    async def get(self, kind, resource_id, actor):
        return json.loads(json.dumps(self.document))

    async def replace(
        self,
        kind,
        resource_id,
        actor,
        document,
        *,
        expected_revision,
    ):
        if self.document["revision"] != expected_revision:
            raise AssertionError("unexpected revision")
        self.document = json.loads(json.dumps(document))
        return json.loads(json.dumps(self.document))


class FixedDouyinPerformanceProvider:
    name = "tikhub"
    configured = True
    configuration_errors = []

    def __init__(self):
        self.calls = 0

    async def _fetch(self, on_usage):
        self.calls += 1
        request_id = f"performance-{self.calls}"
        await on_usage(ProviderUsageRecord(
            usage_id=f"usage-{self.calls}",
            operation="douyin_performance",
            provider="tikhub",
            request_id=request_id,
            succeeded=True,
        ))
        return DouyinVideoPerformance(
            video_id=VIDEO_ID,
            video_url=f"https://www.douyin.com/video/{VIDEO_ID}",
            play_count=1000 * self.calls,
            like_count=100 * self.calls,
            comment_count=10 * self.calls,
            share_count=5 * self.calls,
            collect_count=20 * self.calls,
            video_title="家庭教育测试视频",
            author_name="齐家 AI 家庭教练",
            request_id=request_id,
        )

    async def fetch_by_share_url(self, share_text, *, on_usage=None):
        return await self._fetch(on_usage)

    async def fetch_by_video_id(self, video_id, *, on_usage=None):
        return await self._fetch(on_usage)


class DouyinLinkTests(unittest.TestCase):
    def test_accepts_standard_modal_and_short_douyin_links(self):
        standard, standard_id = normalize_douyin_share_url(
            f"复制链接 https://www.douyin.com/video/{VIDEO_ID} 看作品"
        )
        modal, modal_id = normalize_douyin_share_url(
            f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}"
        )
        short, short_id = normalize_douyin_share_url(
            "3.28 复制打开抖音 https://v.douyin.com/e3x2fjE/"
        )

        self.assertEqual(
            standard, f"https://www.douyin.com/video/{VIDEO_ID}"
        )
        self.assertEqual(standard_id, VIDEO_ID)
        self.assertEqual(modal_id, VIDEO_ID)
        self.assertEqual(short, "https://v.douyin.com/e3x2fjE/")
        self.assertEqual(short_id, "")

    def test_rejects_non_douyin_and_non_video_links_before_paid_request(self):
        with self.assertRaises(QualityGateFailed):
            normalize_douyin_share_url("https://example.com/video/123456")
        with self.assertRaises(QualityGateFailed):
            normalize_douyin_share_url("https://www.douyin.com/user/test")


class TikHubDouyinPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_link_uses_batch_detail_and_records_usage(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "code": 200,
                "request_id": "douyin-request-1",
                "data": {
                    "aweme_list": [{
                        "aweme_id": VIDEO_ID,
                        "desc": "孩子情绪失控时，父母先稳定自己",
                        "author": {"nickname": "齐家 AI 家庭教练"},
                        "statistics": {
                            "play_count": 20007,
                            "digg_count": 1200,
                            "comment_count": 89,
                            "share_count": 34,
                            "collect_count": 218,
                        },
                    }],
                },
            })

        provider = TikHubDouyinPerformanceProvider(
            api_key="test-key",
            base_url="https://api.tikhub.dev",
            transport=httpx.MockTransport(handler),
        )
        usages = []

        async def remember(usage):
            usages.append(usage)

        result = await provider.fetch_by_share_url(
            f"https://www.douyin.com/video/{VIDEO_ID}",
            on_usage=remember,
        )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["path"], "/api/v1/douyin/web/fetch_multi_video"
        )
        self.assertEqual(captured["body"], [VIDEO_ID])
        self.assertEqual(result.play_count, 20007)
        self.assertEqual(result.like_count, 1200)
        self.assertEqual(result.comment_count, 89)
        self.assertEqual(result.share_count, 34)
        self.assertEqual(result.collect_count, 218)
        self.assertEqual(result.request_id, "douyin-request-1")
        self.assertEqual(len(usages), 1)
        self.assertTrue(usages[0].succeeded)
        self.assertEqual(usages[0].operation, "douyin_performance")

    async def test_short_link_uses_official_share_url_endpoint(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["share_url"] = request.url.params["share_url"]
            return httpx.Response(200, json={
                "code": 200,
                "request_id": "douyin-request-2",
                "data": {
                    "aweme_detail": {
                        "aweme_id": VIDEO_ID,
                        "statistics": {"play_count": 88},
                    },
                },
            })

        provider = TikHubDouyinPerformanceProvider(
            api_key="test-key",
            base_url="https://api.tikhub.dev",
            transport=httpx.MockTransport(handler),
        )
        result = await provider.fetch_by_share_url(
            "复制打开抖音 https://v.douyin.com/e3x2fjE/"
        )

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(
            captured["path"],
            "/api/v1/douyin/web/fetch_one_video_by_share_url",
        )
        self.assertEqual(
            captured["share_url"], "https://v.douyin.com/e3x2fjE/"
        )
        self.assertEqual(result.video_id, VIDEO_ID)
        self.assertIsNone(result.like_count)
        self.assertIsNone(result.comment_count)
        self.assertIsNone(result.share_count)
        self.assertIsNone(result.collect_count)

    async def test_paid_response_without_play_count_is_not_saved_as_zero(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "code": 200,
                "request_id": "douyin-request-3",
                "data": {"aweme_detail": {"aweme_id": VIDEO_ID}},
            })

        provider = TikHubDouyinPerformanceProvider(
            api_key="test-key",
            base_url="https://api.tikhub.dev",
            transport=httpx.MockTransport(handler),
        )
        usages = []

        async def remember(usage):
            usages.append(usage)

        with self.assertRaises(ProviderUnavailable):
            await provider.fetch_by_video_id(VIDEO_ID, on_usage=remember)

        self.assertEqual(len(usages), 1)
        self.assertTrue(usages[0].succeeded)
        self.assertEqual(usages[0].request_id, "douyin-request-3")


class DouyinPerformanceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_persists_cost_before_snapshot_and_refresh_appends(self):
        job = VideoJob(
            id="job_service",
            revision=1,
            state=JobState.PACKAGED,
            source_card_id="card_service",
            source_card_revision=1,
            source_card_snapshot={"title": "服务层测试"},
            created_by="editor",
        )
        repository = MemoryJobRepository(job)
        provider = FixedDouyinPerformanceProvider()
        with tempfile.TemporaryDirectory() as directory:
            service = QijiaVideoService(
                repository=repository,
                script_provider=object(),
                storyboard_provider=object(),
                image_provider=object(),
                tts_provider=object(),
                video_provider=object(),
                renderer=object(),
                storage=object(),
                quality_checker=object(),
                media_packager=object(),
                work_root=Path(directory),
                douyin_performance_provider=provider,
                tikhub_price_per_success_usd=0.001,
            )
            actor = Actor(user_id=7, username="editor", role="member")
            bound = await service.bind_douyin_performance(
                job.id,
                f"https://www.douyin.com/video/{VIDEO_ID}",
                1,
                actor,
            )
            refreshed = await service.refresh_douyin_performance(
                job.id,
                bound.revision,
                actor,
            )

        self.assertEqual(bound.revision, 3)
        self.assertEqual(bound.usage_records[0].estimated_currency, "CNY")
        self.assertEqual(bound.usage_records[0].estimated_cost, 0.0067)
        self.assertEqual(bound.douyin_performance.snapshots[0].play_count, 1000)
        self.assertEqual(bound.douyin_performance.snapshots[0].like_count, 100)
        self.assertEqual(refreshed.revision, 5)
        self.assertEqual(
            [item.play_count for item in refreshed.douyin_performance.snapshots],
            [1000, 2000],
        )
        self.assertEqual(
            refreshed.douyin_performance.snapshots[-1].model_dump(
                include={
                    "like_count", "comment_count",
                    "share_count", "collect_count",
                }
            ),
            {
                "like_count": 200,
                "comment_count": 20,
                "share_count": 10,
                "collect_count": 40,
            },
        )
        self.assertEqual(len(refreshed.usage_records), 2)


class DouyinRoiTests(unittest.TestCase):
    def test_target_uses_job_cost_and_includes_playback_query_cost(self):
        observed_at = "2026-08-06T12:00:00+08:00"
        job = VideoJob(
            id="job_roi",
            state=JobState.PACKAGED,
            source_card_id="card_roi",
            source_card_revision=1,
            source_card_snapshot={"title": "孩子发脾气时父母先做什么"},
            usage_records=[
                ProviderUsageRecord(
                    usage_id="production_cost",
                    operation="seedance_video",
                    provider="volcengine-seedance",
                    succeeded=True,
                    estimated_cost=20,
                    estimated_currency="CNY",
                    occurred_at=observed_at,
                ),
                ProviderUsageRecord(
                    usage_id="playback_query_cost",
                    operation="douyin_performance",
                    provider="tikhub",
                    succeeded=True,
                    estimated_cost=0.0067,
                    estimated_currency="CNY",
                    occurred_at=observed_at,
                ),
            ],
            douyin_performance=DouyinPerformance(
                video_id=VIDEO_ID,
                video_url=f"https://www.douyin.com/video/{VIDEO_ID}",
                bound_at=observed_at,
                updated_at=observed_at,
                snapshots=[
                    DouyinPlaybackSnapshot(
                        play_count=20007,
                        like_count=1200,
                        comment_count=89,
                        share_count=34,
                        collect_count=218,
                        observed_at=observed_at,
                        request_id="douyin-request-1",
                    ),
                ],
            ),
        )

        result = build_douyin_performance_analysis(job)

        self.assertEqual(result["accounted_cost_cny"], 20.0067)
        self.assertEqual(result["playback_value_cny"], 200.07)
        self.assertEqual(result["like_count"], 1200)
        self.assertEqual(result["comment_count"], 89)
        self.assertEqual(result["share_count"], 34)
        self.assertEqual(result["collect_count"], 218)
        self.assertEqual(result["target_views"], 20007)
        self.assertEqual(result["remaining_views"], 0)
        self.assertTrue(result["target_achieved"])
        self.assertTrue(result["cost_complete"])
