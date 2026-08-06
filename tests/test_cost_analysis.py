import unittest
from datetime import datetime, timedelta

from qijia_video.contracts import (
    BEIJING_TZ,
    FirstFrameCandidate,
    JobState,
    NarrationAudioSegment,
    NarrationManifest,
    ProviderTask,
    ProviderTaskState,
    ProviderUsageRecord,
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    SEEDANCE_RETIRED_MODEL,
    VideoJob,
)
from qijia_video.cost_analysis import (
    EVENT_DETAIL_LIMIT,
    USD_TO_CNY_RATE,
    build_cost_analysis,
)
from qijia_video.topic_contracts import (
    TikHubCallRecord,
    TopicCostSummary,
    TopicModelUsage,
    TopicResearchRun,
    TopicResearchStatus,
)


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=BEIJING_TZ)
NOW_TEXT = NOW.isoformat(timespec="seconds")


def video_job(*, job_id="job_1", usage_records=None, **updates) -> VideoJob:
    payload = {
        "id": job_id,
        "state": JobState.PACKAGED,
        "source_card_id": "card_1",
        "source_card_revision": 1,
        "source_card_snapshot": {"title": "孩子写作业总要催怎么办"},
        "usage_records": usage_records or [],
        "created_by": "editor_a",
        "created_at": NOW_TEXT,
        "updated_at": NOW_TEXT,
    }
    payload.update(updates)
    return VideoJob.model_validate(payload)


def topic_run() -> TopicResearchRun:
    calls = [
        TikHubCallRecord(endpoint="/trend", succeeded=True),
        TikHubCallRecord(endpoint="/search", succeeded=False),
    ]
    return TopicResearchRun(
        id="topic_1",
        status=TopicResearchStatus.RUNNING,
        cost=TopicCostSummary(
            tikhub_request_budget=10,
            tikhub_request_count=2,
            tikhub_success_count=1,
            tikhub_calls=calls,
            estimated_tikhub_cost_usd=0.001,
            tikhub_cost_basis="$0.001/成功请求",
            model_usage=TopicModelUsage(
                model="openai/gpt-test",
                request_count=1,
                succeeded=True,
                total_tokens=300,
                reported_cost_usd=0.003,
            ),
        ),
        created_by="editor_b",
        created_at=NOW_TEXT,
        updated_at=NOW_TEXT,
    )


class CostAnalysisTests(unittest.TestCase):
    def test_full_ledger_converts_usd_to_the_fixed_cny_reporting_currency(self):
        job = video_job(usage_records=[
            ProviderUsageRecord(
                usage_id="usage_script",
                operation="script_generation",
                provider="openrouter",
                model_id="openai/gpt-test",
                succeeded=True,
                total_tokens=1000,
                reported_cost=0.0123,
                reported_currency="USD",
                occurred_at=NOW_TEXT,
            ),
            ProviderUsageRecord(
                usage_id="usage_image",
                operation="seedream_image",
                provider="volcengine-seedream",
                succeeded=True,
                quantity=1,
                unit="image",
                estimated_cost=0.22,
                estimated_currency="CNY",
                pricing_basis="调用时价格快照",
                occurred_at=NOW_TEXT,
            ),
            ProviderUsageRecord(
                usage_id="usage_video",
                operation="seedance_video",
                provider="volcengine-seedance",
                succeeded=True,
                total_tokens=10000,
                quantity=1,
                unit="video",
                estimated_cost=0.46,
                estimated_currency="CNY",
                pricing_basis="调用时价格快照",
                occurred_at=NOW_TEXT,
            ),
            ProviderUsageRecord(
                usage_id="usage_tts_unknown",
                operation="tts_synthesis",
                provider="volcengine-seed-tts-2.0",
                succeeded=False,
                quantity=120,
                unit="character",
                note="网络异常，待对账",
                occurred_at=NOW_TEXT,
            ),
        ])

        result = build_cost_analysis(
            [job], [topic_run()], days=30, now=NOW,
            seedream_price_per_image=99,
            seedance_price_per_million_tokens=99,
        )
        summary = result["summary"]

        self.assertEqual(USD_TO_CNY_RATE, 6.7)
        self.assertEqual(result["currency"]["display"], "CNY")
        self.assertEqual(result["currency"]["usd_to_cny_rate"], 6.7)
        self.assertAlmostEqual(summary["reported_cny"], 0.10251)
        self.assertAlmostEqual(summary["estimated_cny"], 0.6867)
        self.assertAlmostEqual(summary["accounted_cny"], 0.78921)
        self.assertEqual(summary["event_count"], 7)
        self.assertEqual(summary["unpriced_event_count"], 1)
        self.assertAlmostEqual(summary["coverage_ratio"], 6 / 7, places=4)
        self.assertEqual(summary["completed_video_count"], 1)
        self.assertAlmostEqual(
            summary["video_cost_per_packaged"]["accounted_cny"], 0.76241
        )
        self.assertNotIn("reported_usd", summary)
        self.assertNotIn("estimated_usd", summary)
        self.assertNotIn("accounted_usd", summary)
        self.assertNotIn("accounted_total", summary)
        self.assertTrue(all(
            "reported_usd" not in row and "estimated_usd" not in row
            for row in result["events"]
        ))
        self.assertTrue(all(
            row["currency"] == "CNY" for row in result["pricing"]
        ))
        self.assertTrue(all("$" not in row["note"] for row in result["events"]))
        self.assertEqual({row["key"] for row in result["by_provider"]}, {
            "openrouter", "tikhub", "volcengine-seedream",
            "volcengine-seedance", "volcengine-seed-tts-2.0",
        })

    def test_historical_artifacts_use_current_price_only_when_snapshot_is_missing(self):
        job = video_job(
            usage_records=[],
            first_frame_candidates=[FirstFrameCandidate(
                candidate_id="frame_shot_01_01",
                shot_id="shot_01",
                variant=1,
                prompt="家庭学习空间",
                seed=1,
                model_id="doubao-seedream-5-0-lite-260128",
                created_at=NOW_TEXT,
            )],
            video_tasks=[ProviderTask(
                provider="volcengine-seedance",
                provider_task_id="task_1",
                request_fingerprint="a" * 64,
                model_id="doubao-seedance-2-0-260128",
                state=ProviderTaskState.SUCCEEDED,
                usage_total_tokens=100000,
                created_at=NOW_TEXT,
            )],
            narration_manifest=NarrationManifest(
                provider="volcengine-seed-tts-2.0",
                voice_id="test_voice",
                total_duration_seconds=10,
                full_audio_asset_id="narration_full",
                segments=[NarrationAudioSegment(
                    segment_id="beat_1",
                    text="一二三四",
                    asset_id="narration_full",
                    start_seconds=0,
                    duration_seconds=10,
                )],
            ),
        )

        result = build_cost_analysis(
            [job], [], days=0, now=NOW,
            seedream_price_per_image=0.22,
            seedance_price_per_million_tokens=46,
            tts_price_per_10000_characters=5,
        )

        self.assertAlmostEqual(result["summary"]["estimated_cny"], 4.822)
        self.assertTrue(all(
            row["valuation"] == "estimated_current_price"
            for row in result["events"]
        ))
        self.assertTrue(any("历史" in row["note"] for row in result["events"]))

    def test_historical_seedance_tasks_use_their_own_model_price(self):
        job = video_job(
            usage_records=[],
            video_tasks=[
                ProviderTask(
                    provider="volcengine-seedance",
                    provider_task_id="task_10_fast",
                    request_fingerprint="d" * 64,
                    model_id=SEEDANCE_EFFICIENT_MODEL,
                    state=ProviderTaskState.SUCCEEDED,
                    usage_total_tokens=100000,
                    created_at=NOW_TEXT,
                ),
                ProviderTask(
                    provider="volcengine-seedance",
                    provider_task_id="task_15",
                    request_fingerprint="e" * 64,
                    model_id=SEEDANCE_RETIRED_MODEL,
                    state=ProviderTaskState.SUCCEEDED,
                    usage_total_tokens=100000,
                    created_at=NOW_TEXT,
                ),
                ProviderTask(
                    provider="volcengine-seedance",
                    provider_task_id="task_20",
                    request_fingerprint="f" * 64,
                    model_id=SEEDANCE_FLAGSHIP_MODEL,
                    state=ProviderTaskState.SUCCEEDED,
                    usage_total_tokens=100000,
                    created_at=NOW_TEXT,
                ),
            ],
        )

        result = build_cost_analysis(
            [job],
            [],
            days=0,
            now=NOW,
            seedance_model_prices_per_million_tokens={
                SEEDANCE_EFFICIENT_MODEL: 4.2,
                SEEDANCE_RETIRED_MODEL: 8,
                SEEDANCE_FLAGSHIP_MODEL: 46,
            },
        )

        self.assertAlmostEqual(result["summary"]["estimated_cny"], 5.82)
        self.assertEqual(
            {row["model_id"] for row in result["events"]},
            {
                SEEDANCE_EFFICIENT_MODEL,
                SEEDANCE_RETIRED_MODEL,
                SEEDANCE_FLAGSHIP_MODEL,
            },
        )

    def test_mock_artifacts_are_visible_but_never_charged_as_production(self):
        job = video_job(
            usage_records=[ProviderUsageRecord(
                usage_id="mock_usage",
                operation="tts_synthesis",
                provider="silent-mock",
                model_id="mock-voice",
                succeeded=True,
                quantity=100,
                unit="character",
                estimated_cost=9,
                estimated_currency="CNY",
                occurred_at=NOW_TEXT,
            )],
            first_frame_candidates=[FirstFrameCandidate(
                candidate_id="frame_shot_01_01",
                shot_id="shot_01",
                variant=1,
                prompt="mock",
                seed=1,
                model_id="mock-first-frame",
                created_at=NOW_TEXT,
            )],
            video_tasks=[ProviderTask(
                provider="mock-video",
                provider_task_id="mock_task",
                request_fingerprint="b" * 64,
                state=ProviderTaskState.SUCCEEDED,
                usage_total_tokens=999999,
                created_at=NOW_TEXT,
            )],
        )

        result = build_cost_analysis([job], [], days=0, now=NOW)

        self.assertEqual(result["summary"]["accounted_cny"], 0)
        self.assertEqual(result["summary"]["unpriced_event_count"], 3)
        self.assertTrue(all("测试 Provider" in row["note"] for row in result["events"]))

    def test_time_filter_and_audit_detail_limit_do_not_change_totals(self):
        old_time = (NOW - timedelta(days=40)).isoformat(timespec="seconds")
        old_job = video_job(
            job_id="old_job",
            usage_records=[ProviderUsageRecord(
                usage_id="old_usage",
                operation="script_generation",
                provider="openrouter",
                reported_cost=1,
                reported_currency="USD",
                occurred_at=old_time,
            )],
            created_at=old_time,
            updated_at=old_time,
        )
        recent_records = [ProviderUsageRecord(
            usage_id=f"usage_{index}",
            operation="script_generation",
            provider="openrouter",
            reported_cost=0,
            reported_currency="USD",
            occurred_at=NOW_TEXT,
        ) for index in range(EVENT_DETAIL_LIMIT + 1)]
        recent_job = video_job(job_id="recent_job", usage_records=recent_records)

        result = build_cost_analysis(
            [old_job, recent_job], [], days=30, now=NOW
        )

        self.assertEqual(result["summary"]["event_count"], EVENT_DETAIL_LIMIT + 1)
        self.assertEqual(result["summary"]["accounted_cny"], 0)
        self.assertEqual(len(result["events"]), EVENT_DETAIL_LIMIT)
        self.assertTrue(result["coverage"]["event_detail_limit_reached"])
        self.assertEqual({row["scope_id"] for row in result["content"]}, {"recent_job"})


if __name__ == "__main__":
    unittest.main()
