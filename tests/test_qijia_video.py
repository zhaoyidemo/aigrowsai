import asyncio
import base64
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qijia_video import api as qijia_api
from qijia_video.contracts import (
    Actor,
    AssetRef,
    GenerationSettings,
    JobState,
    PersonResearchBrief,
    PersonViewpointInput,
    QuickSourceCardInput,
    QualityReport,
    RenderManifest,
    ScriptDraft,
    SourceCard,
    SourceCardInput,
    ProviderTaskState,
    ProviderTask,
    ProviderUsageRecord,
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    SEEDANCE_RETIRED_MODEL,
    VideoJob,
    VisualGenerationRequest,
    content_hash,
    timestamp,
)
from qijia_video.errors import (
    AccessDenied,
    ProviderUnavailable,
    QualityGateFailed,
    RevisionConflict,
)
from qijia_video.infrastructure.memory_repository import InMemoryAggregateRepository
from qijia_video.infrastructure.media import FfmpegMediaPackager
from qijia_video.infrastructure.image_providers import (
    MockImageProvider,
    SEEDREAM_MAX_SEED,
    SeedreamImageProvider,
)
from qijia_video.infrastructure.mock_providers import (
    SilentTtsProvider,
    TemplateScriptProvider,
    TemplateStoryboardProvider,
)
from qijia_video.infrastructure.script_providers import (
    OPENROUTER_REASONING_EFFORT,
    OpenRouterScriptProvider,
    OpenRouterStoryboardProvider,
    PERSON_RESEARCH_MAX_COMPLETION_TOKENS,
    PERSON_RESEARCH_PROMPT_VERSION,
    SCRIPT_MAX_COMPLETION_TOKENS,
    SCRIPT_PROMPT_VERSION,
    STORYBOARD_MAX_COMPLETION_TOKENS,
)
from qijia_video.infrastructure.storage import (
    TOS_DOWNLOAD_ATTEMPTS,
    TOS_DOWNLOAD_PART_SIZE_BYTES,
    TOS_DOWNLOAD_TASK_COUNT,
    TOS_SOCKET_TIMEOUT_SECONDS,
    LocalArtifactStorage,
    TosArtifactStorage,
)
from qijia_video.infrastructure.tts_providers import VolcengineTtsProvider
from qijia_video.infrastructure.video_providers import (
    MockVideoProvider,
    SeedanceVideoProvider,
)
from qijia_video.prompts import SCRIPT_HARD_MAX_CHARS, narration_char_count
from qijia_video.service import QijiaVideoService, REQUIRED_PACKAGE_NAMES
from qijia_video import auth as auth_service


def valid_card(**updates) -> SourceCardInput:
    value = {
        "content_domain": "parent_education",
        "content_format": "concept_explainer",
        "subject": {"type": "concept", "name": "自主练习"},
        "title": "如何给孩子自主练习的空间？",
        "core_idea": "能力需要在安全边界内通过练习形成。",
        "parent_question": "什么时候帮助孩子，什么时候让孩子尝试？",
        "sources": [{
            "id": "source_01",
            "type": "book",
            "title": "测试资料",
            "author": "内容团队",
            "publisher": "测试出版社",
            "edition": "2026 年第 1 版",
            "locator": "第 1 章",
            "rights_status": "verified_for_citation",
        }],
        "verified_facts": [{
            "id": "fact_01",
            "text": "这是一条已核验、仅用于工作流测试的事实。",
            "source_refs": ["source_01"],
        }],
        "interpretation_boundary": [{
            "id": "boundary_01",
            "text": "不得表述为对具体儿童的诊断或治疗建议。",
        }],
    }
    value.update(updates)
    return SourceCardInput.model_validate(value)


class FakeRenderer:
    name = "fake-remotion"

    async def render(self, manifest, storage, workspace: Path) -> Path:
        del manifest, storage
        output = workspace / "raw.mp4"
        output.write_bytes(b"raw-video")
        return output

    async def render_cover(self, manifest, storage, workspace: Path) -> Path:
        del manifest, storage
        output = workspace / "cover.jpg"
        output.write_bytes(b"jpeg-cover")
        return output

    def metadata(self) -> dict:
        return {"name": self.name, "remotion_version": "test"}


class FakeMediaPackager:
    name = "fake-ffmpeg"

    async def normalize(self, source: Path, destination: Path) -> Path:
        shutil.copyfile(source, destination)
        return destination

    async def prepare_video_for_timeline(
        self,
        source: Path,
        destination: Path,
        *,
        minimum_duration_seconds: float,
    ) -> tuple[Path, float]:
        shutil.copyfile(source, destination)
        return destination, minimum_duration_seconds + (1 / 30)

class PassingQualityChecker:
    name = "fake-ffprobe"

    async def inspect(self, path: Path, manifest: RenderManifest) -> QualityReport:
        del path, manifest
        return QualityReport(
            automatic_status="review_ready",
            checks=[{"id": "fixture", "passed": True, "detail": "ok"}],
            generated_at=timestamp(),
        )


class RecordingImageProvider(MockImageProvider):
    def __init__(self):
        self.reference_image_urls: list[str] = []
        self.prompts: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_url: str = "",
    ):
        self.reference_image_urls.append(reference_image_url)
        self.prompts.append(prompt)
        return await super().generate(
            prompt,
            seed=seed,
            reference_image_url=reference_image_url,
        )


class QijiaVideoContractTests(unittest.TestCase):
    def test_person_viewpoint_expands_to_internal_content_boundary(self):
        idea = PersonViewpointInput(
            person_name="阿尔弗雷德·阿德勒",
            viewpoint="真正影响孩子的，是孩子如何理解自己在家庭中的位置。",
        )
        card = idea.to_source_card_input()
        self.assertEqual(card.subject.type, "person")
        self.assertEqual(card.subject.name, idea.person_name)
        self.assertEqual(card.content_format.value, "person_idea_explainer")
        self.assertEqual(card.core_idea, idea.viewpoint)
        self.assertEqual(card.verified_facts[0].text, idea.viewpoint)
        self.assertIn("不补造人物经历", card.interpretation_boundary[0].text)

    def test_generation_settings_default_to_ten_images_and_preserve_legacy(self):
        settings = GenerationSettings(
            script_prompt="测试脚本写法",
            seedance_prompt="测试镜头风格",
        )
        self.assertEqual(settings.image_count, 10)
        self.assertEqual(settings.shot_count, 13)
        self.assertEqual(settings.video_resolution, "1080p")
        self.assertEqual(settings.seedance_model, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(
            settings.tts_voice_id,
            "zh_female_vv_uranus_bigtts",
        )
        self.assertEqual(settings.tts_speed_ratio, 1.2)
        legacy_job = VideoJob.model_validate({
            "id": "legacy-resolution-job",
            "state": "card_verified",
            "source_card_id": "legacy-card",
            "source_card_revision": 1,
            "source_card_snapshot": {},
            "generation_settings": {
                "script_prompt": "旧任务脚本写法",
                "seedance_prompt": "旧任务镜头风格",
                "shot_count": 5,
            },
        })
        self.assertEqual(
            legacy_job.generation_settings.video_resolution,
            "480p",
        )
        self.assertEqual(
            legacy_job.generation_settings.seedance_model,
            SEEDANCE_FLAGSHIP_MODEL,
        )
        self.assertEqual(legacy_job.generation_settings.image_count, 2)
        self.assertEqual(legacy_job.generation_settings.shot_count, 5)
        self.assertEqual(legacy_job.generation_settings.tts_speed_ratio, 1.0)
        self.assertEqual(
            {
                GenerationSettings(video_resolution=resolution).video_resolution
                for resolution in ("480p", "720p", "1080p")
            },
            {"480p", "720p", "1080p"},
        )
        with self.assertRaises(ValidationError):
            GenerationSettings(script_prompt="", seedance_prompt="有效镜头提示词")
        with self.assertRaises(ValidationError):
            GenerationSettings(video_resolution="2160p")
        with self.assertRaises(ValidationError):
            GenerationSettings(seedance_model="unknown-seedance")
        with self.assertRaises(ValidationError):
            GenerationSettings(image_count=1)
        with self.assertRaises(ValidationError):
            GenerationSettings(image_count=11)
        with self.assertRaises(ValidationError):
            GenerationSettings(image_count=10, shot_count=5)
        with self.assertRaises(ValidationError):
            GenerationSettings(tts_speed_ratio=1.3)
        with self.assertRaises(ValidationError):
            GenerationSettings(tts_voice_id="unverified-voice")

    def test_legacy_visual_request_fingerprint_does_not_change(self):
        payload = {
            "request_id": "shot_01",
            "prompt": "旧任务镜头",
            "resolution": "480p",
            "ratio": "9:16",
            "duration_seconds": 8,
            "generate_audio": False,
            "seed": None,
            "first_frame_asset_id": "",
        }
        self.assertEqual(
            VisualGenerationRequest.model_validate(payload).fingerprint(),
            content_hash(payload),
        )

    def test_unsubmitted_retired_seedance_requests_migrate_without_touching_paid_tasks(self):
        retired_request = VisualGenerationRequest(
            request_id="shot_01",
            prompt="旧模型未提交成功的镜头",
            model_id=SEEDANCE_RETIRED_MODEL,
            resolution="1080p",
        )
        base_payload = {
            "id": "retired-seedance-job",
            "state": "failed",
            "source_card_id": "card_01",
            "source_card_revision": 1,
            "source_card_snapshot": {},
            "generation_settings": {
                "seedance_model": SEEDANCE_RETIRED_MODEL,
            },
            "visual_requests": [retired_request.model_dump(mode="json")],
        }

        unsubmitted = VideoJob.model_validate(base_payload)
        self.assertEqual(
            unsubmitted.generation_settings.seedance_model,
            SEEDANCE_EFFICIENT_MODEL,
        )
        self.assertEqual(
            unsubmitted.visual_requests[0].model_id,
            SEEDANCE_EFFICIENT_MODEL,
        )

        new_draft = VideoJob(
            **{
                key: value
                for key, value in base_payload.items()
                if key not in {"generation_settings", "visual_requests"}
            },
            generation_settings=GenerationSettings(
                seedance_model=SEEDANCE_RETIRED_MODEL,
            ),
        )
        self.assertEqual(
            new_draft.generation_settings.seedance_model,
            SEEDANCE_EFFICIENT_MODEL,
        )

        submitted_payload = dict(base_payload)
        submitted_payload["video_tasks"] = [{
            "provider": "volcengine-seedance",
            "provider_task_id": "paid_task_01",
            "request_fingerprint": retired_request.fingerprint(),
            "request_id": retired_request.request_id,
            "model_id": SEEDANCE_RETIRED_MODEL,
            "state": "running",
        }]
        submitted = VideoJob.model_validate(submitted_payload)
        self.assertEqual(
            submitted.generation_settings.seedance_model,
            SEEDANCE_RETIRED_MODEL,
        )
        self.assertEqual(
            submitted.visual_requests[0].model_id,
            SEEDANCE_RETIRED_MODEL,
        )

    def test_default_seedance_style_is_a_continuous_hybrid_story(self):
        prompt = GenerationSettings().seedance_prompt
        self.assertIn("编辑插画动画", prompt)
        self.assertIn("同一组虚构东亚家庭成员", prompt)
        self.assertIn("始终一致", prompt)
        self.assertIn("图片镜头", prompt)
        self.assertIn("2.5D", prompt)

    def test_subtitle_cues_are_normalized_and_limited_to_one_line(self):
        chunks = QijiaVideoService._split_subtitle_text(
            "父母先停一下，\n让孩子把自己的想法完整说出来，再一起讨论下一步。"
        )
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertTrue(all("\n" not in chunk for chunk in chunks))
        self.assertEqual(
            "".join(chunks),
            "父母先停一下，让孩子把自己的想法完整说出来，再一起讨论下一步。",
        )

    def test_default_script_prompt_targets_retention_without_clickbait(self):
        prompt = GenerationSettings().script_prompt
        self.assertIn("降低 2 秒流失率、提高 5 秒完播率", prompt)
        self.assertIn("反常识翻转", prompt)
        self.assertIn("因果反转", prompt)
        self.assertIn("具体场景冲突", prompt)
        self.assertIn("不得用“毁掉孩子”", prompt)
        self.assertIn("第一段画面从正在发生的动作", prompt)
        self.assertIn("贯穿全片的可见变化", prompt)
        self.assertIn("画面不能只是旁白的图解", prompt)
        self.assertIn("结尾画面回应、完成或反转开场", prompt)
        self.assertIn("不要额外输出导演说明", prompt)

    def test_longest_later_chapters_become_images(self):
        self.assertEqual(
            QijiaVideoService._visual_types_for_durations(
                [7.23, 9.53, 6.87, 10.47, 13.9]
            ),
            ("video", "video", "video", "image", "image"),
        )

    def test_contract_rejects_unknown_schema_version(self):
        with self.assertRaises(ValidationError):
            valid_card(schema_version="2.0")

    def test_source_card_rejects_unknown_source_reference(self):
        with self.assertRaises(ValidationError):
            valid_card(verified_facts=[{
                "id": "fact_01",
                "text": "事实",
                "source_refs": ["missing"],
            }])

    def test_source_card_accepts_only_one_global_reference_image(self):
        image_asset = {
            "asset_id": "reference_image_test",
            "object_key": "qijia-video/reference-images/test.png",
            "sha256": "a" * 64,
            "size_bytes": 128,
            "media_type": "image/png",
        }
        card = valid_card(reference_assets=[image_asset])
        self.assertEqual(card.reference_assets[0]["asset_id"], "reference_image_test")
        with self.assertRaises(ValidationError):
            valid_card(reference_assets=[image_asset, image_asset])
        with self.assertRaises(ValidationError):
            valid_card(reference_assets=[{
                **image_asset,
                "media_type": "video/mp4",
            }])

    def test_quick_source_card_expands_to_traceable_internal_contract(self):
        quick = QuickSourceCardInput.model_validate({
            "title": "孩子遇到困难时，父母应该立刻帮忙吗？",
            "source_material": (
                "https://example.com/article "
                "儿童需要在安全边界内获得持续、可承担的自主练习。"
            ),
            "rights_confirmed": True,
        })
        card = quick.to_source_card_input()
        self.assertEqual(card.subject.type, "concept")
        self.assertEqual(card.parent_question, quick.title)
        self.assertEqual(card.sources[0].url, "https://example.com/article")
        self.assertEqual(card.sources[0].rights_status, "verified_for_citation")
        self.assertTrue(card.sources[0].accessed_at)
        self.assertNotIn("https://", card.verified_facts[0].text)
        self.assertIn("诊断或治疗", card.interpretation_boundary[0].text)

    def test_quick_source_card_requires_content_beyond_a_link_and_confirmation(self):
        with self.assertRaises(ValidationError):
            QuickSourceCardInput.model_validate({
                "title": "一个选题",
                "source_material": "https://example.com/article",
                "rights_confirmed": True,
            })
        with self.assertRaises(ValidationError):
            QuickSourceCardInput.model_validate({
                "title": "一个选题",
                "source_material": "这是一段已经由用户核对过的关键参考内容。",
                "rights_confirmed": False,
            })

    def test_render_manifest_defaults_to_no_embedded_ai_label_or_brand(self):
        audio = {
            "asset_id": "audio",
            "object_key": "audio.wav",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "audio/wav",
        }
        base = {
            "job_id": "job",
            "duration_in_frames": 1350,
            "assets": [audio],
            "audio_asset_id": "audio",
            "visual_blocks": [{
                "id": "v1",
                "type": "title_card",
                "start_frame": 0,
                "duration_in_frames": 1350,
            }],
        }
        manifest = RenderManifest(**base)
        self.assertFalse(manifest.ai_content_label.enabled)
        # Read compatibility for manifests saved before embedded labels were removed.
        legacy = RenderManifest(**base, ai_content_label={"enabled": True})
        self.assertTrue(legacy.ai_content_label.enabled)
        with self.assertRaises(ValidationError):
            RenderManifest(**base, brand_overlay={"text": "齐家 AI"})

    def test_render_manifest_accepts_three_vertical_resolution_pairs(self):
        audio = {
            "asset_id": "audio",
            "object_key": "audio.wav",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "audio/wav",
        }
        base = {
            "job_id": "job",
            "duration_in_frames": 1350,
            "assets": [audio],
            "audio_asset_id": "audio",
            "visual_blocks": [{
                "id": "v1",
                "type": "title_card",
                "start_frame": 0,
                "duration_in_frames": 1350,
            }],
        }
        manifest = RenderManifest(**base)
        self.assertEqual((manifest.width, manifest.height), (480, 854))
        hd = RenderManifest(**base, width=720, height=1280)
        self.assertEqual((hd.width, hd.height), (720, 1280))
        full_hd = RenderManifest(**base, width=1080, height=1920)
        self.assertEqual((full_hd.width, full_hd.height), (1080, 1920))
        with self.assertRaises(ValidationError):
            RenderManifest(**base, width=720, height=1920)

    def test_script_v1_hash_is_stable_while_v2_has_independent_tracks(self):
        old_payload = {
            "schema_version": "1.0",
            "source_card_id": "card-old",
            "source_card_revision": 1,
            "video_title": "旧脚本",
            "cover_text": "旧封面",
            "hook": "第一段",
            "narration_segments": [
                {"id": "n01", "text": "第一段", "segment_type": "hook", "source_refs": ["fact_01"], "quote_ref": None},
                {"id": "n02", "text": "第二段", "segment_type": "explanation", "source_refs": ["fact_01"], "quote_ref": None},
                {"id": "n03", "text": "第三段", "segment_type": "closing", "source_refs": ["fact_01"], "quote_ref": None},
            ],
            "closing": "第三段",
            "estimated_duration_seconds": 45,
            "caption": "旧脚本",
            "hashtags": [],
        }
        restored = ScriptDraft.model_validate(old_payload)
        self.assertEqual(content_hash(restored), content_hash(old_payload))
        self.assertEqual(restored.beats[0].narration, "第一段")
        self.assertEqual(restored.model_dump(mode="json"), old_payload)

        v2_payload = {
            **old_payload,
            "schema_version": "2.0",
            "beats": [
                {
                    "id": f"n{index:02d}",
                    "role": role,
                    "narration": f"旁白 {index}",
                    "visual_direction": f"画面 {index}",
                    "on_screen_text": "重点" if index == 1 else "",
                    "source_refs": ["fact_01"],
                    "quote_ref": None,
                }
                for index, role in enumerate(
                    ["hook", "context", "explanation", "application", "closing"], 1
                )
            ],
        }
        v2_payload.pop("narration_segments")
        current = ScriptDraft.model_validate(v2_payload)
        self.assertIn("beats", current.model_dump(mode="json"))
        self.assertEqual(current.hook, "旁白 1")
        self.assertEqual(current.closing, "旁白 5")


class SeedanceProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_status_and_bounded_download_use_official_task_shape(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["model"], SEEDANCE_EFFICIENT_MODEL)
                self.assertEqual(body["resolution"], "480p")
                self.assertEqual(body["ratio"], "9:16")
                self.assertEqual(body["duration"], 8)
                self.assertFalse(body["generate_audio"])
                self.assertFalse(body["watermark"])
                self.assertEqual(body["content"][1], {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://media.volces.com/frame.jpg",
                    },
                    "role": "first_frame",
                })
                return httpx.Response(
                    200, json={"id": "cgt-1", "status": "queued"}
                )
            if request.url.host == "media.volces.com":
                return httpx.Response(
                    200,
                    content=b"video-bytes",
                    headers={
                        "content-type": "video/mp4",
                        "content-length": "11",
                    },
                )
            return httpx.Response(200, json={
                "id": "cgt-1",
                "model": SEEDANCE_EFFICIENT_MODEL,
                "status": "succeeded",
                "content": {"video_url": "https://media.volces.com/result.mp4"},
                "usage": {"total_tokens": 40500},
            })

        provider = SeedanceVideoProvider(
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        request = VisualGenerationRequest(
            request_id="shot_01",
            prompt="克制的家庭生活场景",
            model_id=SEEDANCE_EFFICIENT_MODEL,
            first_frame_asset_id="first_frame_frame_shot_01_01",
        )
        fingerprint = request.fingerprint()
        submitted = await provider.submit(
            request,
            first_frame_url="https://media.volces.com/frame.jpg",
        )
        self.assertEqual(submitted.state, ProviderTaskState.QUEUED)
        self.assertEqual(submitted.model_id, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(submitted.request_fingerprint, fingerprint)
        status = await provider.get_status("cgt-1", request.fingerprint())
        self.assertEqual(status.state, ProviderTaskState.SUCCEEDED)
        self.assertEqual(status.model_id, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(status.usage_total_tokens, 40500)
        with tempfile.TemporaryDirectory() as directory:
            output = await provider.download("cgt-1", Path(directory) / "shot.mp4")
            self.assertEqual(output.read_bytes(), b"video-bytes")
        self.assertEqual(sum(item.method == "POST" for item in requests), 1)
        media_request = next(
            item for item in requests if item.url.host == "media.volces.com"
        )
        self.assertNotIn("authorization", media_request.headers)

    async def test_ambiguous_submit_failure_is_not_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("unknown", request=request)

        provider = SeedanceVideoProvider(
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(Exception, "避免重复扣费"):
            await provider.submit(VisualGenerationRequest(
                request_id="shot_01", prompt="测试镜头"
            ))
        self.assertEqual(calls, 1)


class SeedreamProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_and_download_use_official_image_shape(self):
        requests = []
        image_bytes = b"\x89PNG\r\n\x1a\nfixture"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(
                    body["model"], "doubao-seedream-5-0-lite-260128"
                )
                self.assertEqual(body["size"], "1440x2560")
                self.assertEqual(body["seed"], 42)
                self.assertEqual(body["sequential_image_generation"], "disabled")
                self.assertEqual(body["response_format"], "url")
                self.assertFalse(body["watermark"])
                self.assertEqual(
                    body["image"],
                    ["https://private.volces.com/reference.png"],
                )
                return httpx.Response(200, json={
                    "model": "doubao-seedream-5-0-lite-260128",
                    "data": [{
                        "url": "https://media.volces.com/frame.png",
                        "size": "1440x2560",
                    }],
                    "usage": {"total_tokens": 1234},
                })
            return httpx.Response(
                200,
                content=image_bytes,
                headers={
                    "content-type": "image/png",
                    "content-length": str(len(image_bytes)),
                },
            )

        provider = SeedreamImageProvider(
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            size="1440x2560",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        generated = await provider.generate(
            "竖屏动画首帧",
            seed=42,
            reference_image_url="https://private.volces.com/reference.png",
        )
        self.assertEqual(generated.usage_total_tokens, 1234)
        with tempfile.TemporaryDirectory() as directory:
            output = await provider.download(
                generated.url, Path(directory) / "frame.png"
            )
            self.assertEqual(output.read_bytes(), image_bytes)
        self.assertEqual(sum(item.method == "POST" for item in requests), 1)
        media_request = next(
            item for item in requests if item.url.host == "media.volces.com"
        )
        self.assertNotIn("authorization", media_request.headers)

    async def test_ambiguous_image_submit_is_not_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("unknown", request=request)

        provider = SeedreamImageProvider(
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            size="1440x2560",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderUnavailable, "避免重复扣费"):
            await provider.generate("测试首帧", seed=1)
        self.assertEqual(calls, 1)

    async def test_seedream_rejects_seed_outside_signed_int32_before_submit(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        provider = SeedreamImageProvider(
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            size="1440x2560",
            allowed_download_hosts=(".volces.com",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(ProviderUnavailable, "int32"):
            await provider.generate(
                "测试首帧", seed=SEEDREAM_MAX_SEED + 1
            )
        self.assertEqual(calls, 0)


class RealProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_packager_normalizes_audio_without_reencoding_video(self):
        packager = FfmpegMediaPackager()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rendered.mp4"
            destination = root / "ready.mp4"
            source.write_bytes(b"rendered")

            async def fake_run(*args, **kwargs):
                del kwargs
                Path(args[-1]).write_bytes(b"remuxed")
                return "ok"

            runner = AsyncMock(side_effect=fake_run)
            with (
                patch(
                    "qijia_video.infrastructure.media.shutil.which",
                    return_value="ffmpeg",
                ),
                patch.object(packager, "_run", runner),
            ):
                result = await packager.normalize(source, destination)

        self.assertEqual(result, destination)
        arguments = runner.await_args.args
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "copy")
        self.assertEqual(
            arguments[arguments.index("-af") + 1],
            "loudnorm=I=-16:TP=-1.5:LRA=7",
        )
        self.assertEqual(arguments[arguments.index("-c:a") + 1], "aac")
        self.assertEqual(arguments[arguments.index("-b:a") + 1], "192k")
        self.assertEqual(arguments[arguments.index("-ar") + 1], "48000")
        self.assertIn("+faststart", arguments)
        self.assertNotIn("libx264", arguments)

    async def test_media_packager_holds_last_frame_instead_of_slowing_video(self):
        packager = FfmpegMediaPackager()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "seedance.mp4"
            destination = root / "timeline.mp4"
            source.write_bytes(b"seedance")

            async def fake_run(*args, **kwargs):
                del kwargs
                Path(args[-1]).write_bytes(b"prepared")
                return "ok"

            runner = AsyncMock(side_effect=fake_run)
            with (
                patch(
                    "qijia_video.infrastructure.media.shutil.which",
                    return_value="ffmpeg",
                ),
                patch.object(
                    packager,
                    "_probe_duration",
                    AsyncMock(side_effect=[8.0, 13.95]),
                ),
                patch.object(packager, "_run", runner),
            ):
                result, duration = await packager.prepare_video_for_timeline(
                    source,
                    destination,
                    minimum_duration_seconds=13.9,
                )

        self.assertEqual(result, destination)
        self.assertEqual(duration, 13.95)
        arguments = runner.await_args.args
        self.assertIn("tpad=stop_mode=clone", " ".join(arguments))
        self.assertIn("libx264", arguments)
        self.assertNotIn("setpts", " ".join(arguments))

    async def test_tts_limit_fallback_uses_minimum_chunks(self):
        texts = ["甲" * 200 + "。", "乙" * 200 + "。"]
        chunks = VolcengineTtsProvider._synthesis_chunks(texts)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(item.encode("utf-8")) <= 1000 for item in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), "".join(texts))

    async def test_unconfigured_tos_is_reportable_without_startup_failure(self):
        storage = TosArtifactStorage(
            access_key_id="",
            secret_access_key="",
            bucket="",
            region="cn-shanghai",
        )
        self.assertFalse(storage.configured)
        with self.assertRaises(ProviderUnavailable):
            storage._client()

    async def test_tos_allows_slow_video_transfers_and_keeps_sdk_retries(self):
        storage = TosArtifactStorage(
            access_key_id="test-ak",
            secret_access_key="test-sk",
            bucket="test-bucket",
            region="cn-shanghai",
        )
        with patch("tos.TosClientV2") as client_class:
            storage._client()
        kwargs = client_class.call_args.kwargs
        self.assertEqual(kwargs["max_retry_count"], 3)
        self.assertEqual(kwargs["socket_timeout"], TOS_SOCKET_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["request_timeout"], TOS_SOCKET_TIMEOUT_SECONDS)

    async def test_tos_download_resumes_with_checkpoint_after_read_timeouts(self):
        payload = b"seedance-video" * 1024
        asset = AssetRef(
            asset_id="visual_shot_01",
            object_key="qijia-video/job/video/shot_01.mp4",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="video/mp4",
        )
        storage = TosArtifactStorage(
            access_key_id="test-ak",
            secret_access_key="test-sk",
            bucket="test-bucket",
            region="cn-shanghai",
        )

        class FlakyDownloadClient:
            def __init__(self):
                self.calls: list[dict] = []

            def download_file(self, bucket, key, file_path, **kwargs):
                self.calls.append({
                    "bucket": bucket,
                    "key": key,
                    "file_path": file_path,
                    **kwargs,
                })
                if len(self.calls) < TOS_DOWNLOAD_ATTEMPTS:
                    raise TimeoutError("Read timed out")
                Path(file_path).write_bytes(payload)

        client = FlakyDownloadClient()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            storage, "_client", return_value=client
        ), patch(
            "qijia_video.infrastructure.storage.asyncio.sleep",
            new=AsyncMock(),
        ):
            destination = Path(directory) / "shot_01.mp4"
            result = await storage.materialize(asset, destination)
            downloaded = result.read_bytes()

        self.assertEqual(downloaded, payload)
        self.assertEqual(len(client.calls), TOS_DOWNLOAD_ATTEMPTS)
        self.assertTrue(all(
            item["enable_checkpoint"] is True
            and item["part_size"] == TOS_DOWNLOAD_PART_SIZE_BYTES
            and item["task_num"] == TOS_DOWNLOAD_TASK_COUNT
            for item in client.calls
        ))

    def test_reference_normalization_does_not_hide_ambiguous_or_unknown_ids(self):
        card = SourceCard(
            **valid_card(verified_facts=[
                {
                    "id": "fact_01",
                    "text": "第一条事实",
                    "source_refs": ["source_01"],
                },
                {
                    "id": "fact_02",
                    "text": "第二条事实",
                    "source_refs": ["source_01"],
                },
            ]).model_dump(mode="json"),
            id="card-ambiguous-reference",
            revision=1,
            status="verified",
        )
        generated = {
            "beats": [{
                "source_refs": ["source_01", "invented_01"],
                "quote_ref": None,
            }]
        }

        OpenRouterScriptProvider._normalize_generated_source_refs(card, generated)

        self.assertEqual(
            generated["beats"][0]["source_refs"],
            ["source_01", "invented_01"],
        )

    async def test_openrouter_generates_contract_in_exactly_one_call(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            generated = {
                "schema_version": "2.0",
                "video_title": "帮助之前，先给孩子一点尝试空间",
                "cover_text": "别急着替孩子完成",
                "hook": "孩子卡住时，父母越快出手就越好吗？",
                "beats": [
                    {
                        "id": f"n{index:02d}",
                        "narration": text.ljust(40, "。"),
                        "role": kind,
                        "visual_direction": f"第 {index} 段具体家庭动作，无文字",
                        "on_screen_text": "" if index > 1 else "先别急着出手",
                        # Models sometimes confuse the source record with its
                        # single supported fact. The provider must repair this
                        # deterministic metadata without another model call.
                        "source_refs": ["source_01"],
                        "quote_ref": None,
                    }
                    for index, (kind, text) in enumerate([
                        ("hook", "孩子卡住时，父母越快出手就越好吗？"),
                        ("context", "帮助的关键，不只是眼前有没有完成。"),
                        ("explanation", "能力需要在安全边界内通过练习逐渐形成。"),
                        ("application", "先判断风险，再给孩子一小段可以承担的尝试时间。"),
                        ("closing", "今天先少替孩子完成一步，多观察一步。"),
                    ], 1)
                ],
                "closing": "把答案留给下一次真实的家庭互动。",
                "caption": "帮助孩子，不等于替孩子完成。",
                "hashtags": ["家庭教育", "家长成长", "亲子沟通"],
            }
            return httpx.Response(200, json={
                "id": "generation-script-1",
                "model": "resolved/test-model",
                "choices": [{"message": {"content": json.dumps(
                    generated, ensure_ascii=False
                )}}],
                "usage": {
                    "prompt_tokens": 800,
                    "completion_tokens": 200,
                    "total_tokens": 1000,
                    "cost": 0.0123,
                    "prompt_tokens_details": {"cached_tokens": 120},
                    "completion_tokens_details": {"reasoning_tokens": 40},
                },
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-1",
            revision=2,
            status="verified",
        )
        usage_records: list[ProviderUsageRecord] = []

        async def record_usage(usage: ProviderUsageRecord) -> None:
            usage_records.append(usage)

        script = await provider.generate_with_usage(
            card, on_usage=record_usage
        )
        review = await provider.review(card, script)
        self.assertEqual(len(calls), 1)
        request_body = json.loads(calls[0].content)
        response_format = request_body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(
            response_format["json_schema"]["name"],
            "qijia_script_draft_v2",
        )
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertFalse(
            response_format["json_schema"]["schema"]["additionalProperties"]
        )
        self.assertEqual(
            request_body["plugins"], [{"id": "response-healing"}]
        )
        self.assertTrue(request_body["provider"]["require_parameters"])
        self.assertEqual(
            request_body["reasoning"]["effort"],
            OPENROUTER_REASONING_EFFORT,
        )
        self.assertTrue(request_body["reasoning"]["exclude"])
        self.assertEqual(
            request_body["max_completion_tokens"],
            SCRIPT_MAX_COMPLETION_TOKENS,
        )
        self.assertNotIn("max_tokens", request_body)
        self.assertNotIn("temperature", request_body)
        prompt = request_body["messages"][1]["content"]
        self.assertIn("一条视频只讲清一个核心观点", prompt)
        self.assertIn("语言自然、具体，有思考感但不说教", prompt)
        self.assertIn("非共识价值", prompt)
        self.assertIn("不要写成模板化的五段论", prompt)
        self.assertIn("降低 2 秒流失率、提高 5 秒完播率", prompt)
        self.assertIn("强钩子设计", prompt)
        self.assertIn("导演思维", prompt)
        self.assertIn("可复用的判断框架", prompt)
        self.assertIn("具体、低压力、无需暴露隐私", prompt)
        self.assertIn("5-8 段", prompt)
        self.assertIn("narration 是唯一会送入 TTS 的口播", prompt)
        self.assertIn(
            'beats.source_refs 只能从这里选择）：["fact_01"]', prompt
        )
        self.assertIn(
            "source ID 只用于材料溯源，绝不能填写到 beats.source_refs", prompt
        )
        self.assertIn('"title": "测试资料"', prompt)
        self.assertEqual(script.source_card_id, card.id)
        self.assertTrue(all(
            beat.source_refs == ["fact_01"] for beat in script.beats
        ))
        self.assertEqual(script.hook, script.narration_segments[0].text)
        self.assertEqual(script.closing, script.narration_segments[-1].text)
        self.assertEqual(narration_char_count(script.narration_text()), 200)
        self.assertTrue(review.passed)
        self.assertEqual(review.input_hash, content_hash(script))
        self.assertEqual(review.prompt_version, SCRIPT_PROMPT_VERSION)
        self.assertEqual(len(usage_records), 1)
        self.assertTrue(usage_records[0].succeeded)
        self.assertEqual(usage_records[0].request_id, "generation-script-1")
        self.assertEqual(usage_records[0].model_id, "resolved/test-model")
        self.assertEqual(usage_records[0].total_tokens, 1000)
        self.assertEqual(usage_records[0].cached_tokens, 120)
        self.assertEqual(usage_records[0].reasoning_tokens, 40)
        self.assertEqual(usage_records[0].reported_currency, "USD")
        self.assertAlmostEqual(usage_records[0].reported_cost, 0.0123)
        custom_prompt = provider._prompt(card, "只用短句，语气平静。")
        self.assertTrue(custom_prompt.startswith("只用短句，语气平静。"))
        self.assertIn("【系统输出格式】", custom_prompt)
        self.assertIn("【本次来源卡】", custom_prompt)

    async def test_person_research_uses_bounded_web_search_and_cited_evidence(self):
        calls: list[httpx.Request] = []
        cited_url = "https://example.edu/primary-source"
        uncited_url = "https://untrusted.example/unsupported"

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            generated = {
                "summary": "该人物的思想可以帮助家长区分支持与替代。",
                "core_tension": "及时帮助和保留自主空间之间存在真实张力。",
                "audience_relevance": [
                    "孩子遇到困难时，家长常在立即接手和继续观察之间摇摆。",
                    "不同风险场景需要不同程度的支持。",
                ],
                "content_angles": [
                    "先判断风险，再决定帮助到哪一步。",
                    "把支持拆成提示、示范和代办三个层级。",
                ],
                "interaction_opportunity": "你更容易在哪类任务里过早接手？",
                "evidence": [
                    {
                        "claim": "可靠原始资料支持的背景事实。",
                        "source_title": "模型填写但应由注释覆盖的标题",
                        "source_url": cited_url,
                    },
                    {
                        "claim": "没有检索注释支持的内容不能进入简报。",
                        "source_title": "未核验页面",
                        "source_url": uncited_url,
                    },
                ],
                "uncertainties": ["该观点不能表述为人物逐字原话。"],
            }
            return httpx.Response(200, json={
                "id": "generation-research-1",
                "model": "resolved/research-model",
                "choices": [{
                    "message": {
                        "content": json.dumps(generated, ensure_ascii=False),
                        "annotations": [{
                            "type": "url_citation",
                            "url_citation": {
                                "url": cited_url,
                                "title": "大学原始资料",
                                "content": "与主题相关的原始资料摘要。",
                            },
                        }],
                    },
                }],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 300,
                    "total_tokens": 1200,
                    "cost": 0.045,
                    "server_tool_use": {"web_search_requests": 2},
                },
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/research-model",
            transport=httpx.MockTransport(handler),
        )
        card_input = PersonViewpointInput(
            person_name="阿尔弗雷德·阿德勒",
            viewpoint="真正影响孩子的，是孩子如何理解自己在家庭中的位置。",
        ).to_source_card_input()
        card = SourceCard(
            **card_input.model_dump(mode="json"),
            id="card-person-research",
            revision=1,
            status="verified",
        )
        usage_records: list[ProviderUsageRecord] = []

        async def record_usage(usage: ProviderUsageRecord) -> None:
            usage_records.append(usage)

        brief = await provider.research_person_viewpoint(
            card, on_usage=record_usage
        )

        self.assertEqual(len(calls), 1)
        request_body = json.loads(calls[0].content)
        self.assertEqual(
            request_body["reasoning"]["effort"],
            OPENROUTER_REASONING_EFFORT,
        )
        self.assertEqual(
            request_body["max_completion_tokens"],
            PERSON_RESEARCH_MAX_COMPLETION_TOKENS,
        )
        self.assertEqual(request_body["max_tool_calls"], 2)
        tool = request_body["tools"][0]
        self.assertEqual(tool["type"], "openrouter:web_search")
        self.assertEqual(tool["parameters"]["engine"], "exa")
        self.assertEqual(tool["parameters"]["mode"], "deep-lite")
        self.assertEqual(tool["parameters"]["max_uses"], 2)
        self.assertEqual(tool["parameters"]["max_total_results"], 8)
        prompt = request_body["messages"][1]["content"]
        self.assertIn("研究日期（UTC）", prompt)
        self.assertIn("至少使用两个彼此独立的查询", prompt)
        self.assertEqual(brief.person_name, card.subject.name)
        self.assertEqual(brief.viewpoint, card.core_idea)
        self.assertEqual(brief.prompt_version, PERSON_RESEARCH_PROMPT_VERSION)
        self.assertEqual(brief.model_id, "resolved/research-model")
        self.assertEqual(len(brief.evidence), 1)
        self.assertEqual(brief.evidence[0].source_url, cited_url)
        self.assertEqual(brief.evidence[0].source_title, "大学原始资料")
        self.assertEqual(len(usage_records), 1)
        self.assertEqual(usage_records[0].operation, "person_research")
        self.assertIn("联网检索 2 次", usage_records[0].note)

    async def test_person_research_rejects_evidence_without_search_citations(self):
        def handler(_: httpx.Request) -> httpx.Response:
            generated = {
                "summary": "看似完整但没有引用注释的研究结果。",
                "core_tension": "帮助与替代之间的张力。",
                "audience_relevance": ["家长需要判断帮助边界。"],
                "content_angles": ["先看风险，再决定介入程度。"],
                "interaction_opportunity": "你会在哪一步停下来观察？",
                "evidence": [{
                    "claim": "没有注释支持的事实。",
                    "source_title": "未知页面",
                    "source_url": "https://example.com/uncited",
                }],
                "uncertainties": [],
            }
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "content": json.dumps(generated, ensure_ascii=False),
                }}],
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/research-model",
            transport=httpx.MockTransport(handler),
        )
        card_input = PersonViewpointInput(
            person_name="阿尔弗雷德·阿德勒",
            viewpoint="真正影响孩子的，是孩子如何理解自己在家庭中的位置。",
        ).to_source_card_input()
        card = SourceCard(
            **card_input.model_dump(mode="json"),
            id="card-uncited-research",
            revision=1,
            status="verified",
        )

        with self.assertRaisesRegex(ProviderUnavailable, "检索注释匹配"):
            await provider.research_person_viewpoint(card)

    async def test_openrouter_reports_truncated_json_with_the_exact_stage(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"x-request-id": "req-truncated-script"},
                json={
                    "choices": [{
                        "message": {
                            "content": '{"schema_version":"2.0","beats":['
                        },
                        "finish_reason": "length",
                    }]
                },
            )

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-truncated-script",
            revision=1,
            status="verified",
        )

        with self.assertRaisesRegex(
            ProviderUnavailable,
            "OpenRouter 脚本生成输出被截断.*req-truncated-script",
        ):
            await provider.generate(card)

    async def test_openrouter_keeps_a_complete_over_target_script_in_one_call(self):
        calls: list[httpx.Request] = []

        def generated(segment_text: str) -> dict:
            kinds = ["hook", "context", "explanation", "application", "closing"]
            return {
                "schema_version": "2.0",
                "video_title": "给孩子留出尝试空间",
                "cover_text": "帮助不等于替代",
                "hook": segment_text,
                "beats": [
                    {
                        "id": f"n{index:02d}",
                        "narration": segment_text,
                        "role": kind,
                        "visual_direction": f"第 {index} 段家庭动作，无文字",
                        "on_screen_text": "",
                        "source_refs": ["fact_01"],
                        "quote_ref": None,
                    }
                    for index, kind in enumerate(kinds, 1)
                ],
                "closing": segment_text,
                "caption": "给孩子留出尝试空间。",
                "hashtags": ["家庭教育", "亲子沟通", "家长成长"],
            }

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            result = generated("长" * 80)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(
                    result, ensure_ascii=False
                )}}]
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-long-script",
            revision=1,
            status="verified",
        )
        script = await provider.generate(card)

        self.assertEqual(len(calls), 1)
        self.assertEqual(narration_char_count(script.narration_text()), 400)
        self.assertEqual(script.estimated_duration_seconds, 75)

    async def test_openrouter_keeps_a_concise_script_in_one_call(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-short-script",
            revision=1,
            status="verified",
        )
        calls: list[httpx.Request] = []

        def generated(segment_text: str) -> dict:
            kinds = ["hook", "context", "explanation", "application", "closing"]
            return {
                "schema_version": "2.0",
                "video_title": "给孩子留出尝试空间",
                "cover_text": "帮助不等于替代",
                "hook": segment_text,
                "beats": [
                    {
                        "id": f"n{index:02d}",
                        "narration": segment_text,
                        "role": kind,
                        "visual_direction": f"第 {index} 段家庭动作，无文字",
                        "on_screen_text": "",
                        "source_refs": ["fact_01"],
                        "quote_ref": None,
                    }
                    for index, kind in enumerate(kinds, 1)
                ],
                "closing": segment_text,
                "caption": "给孩子留出尝试空间。",
                "hashtags": ["家庭教育", "亲子沟通", "家长成长"],
            }

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            result = generated("短" * 20)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(
                    result, ensure_ascii=False
                )}}]
            })

        provider = OpenRouterScriptProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/model",
            transport=httpx.MockTransport(handler),
        )
        script = await provider.generate(card)

        self.assertEqual(len(calls), 1)
        self.assertEqual(narration_char_count(script.narration_text()), 100)

    async def test_openrouter_storyboard_keeps_structured_shot_ids(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-storyboard",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        target_ids = [item.id for item in script.narration_segments]
        beat_groups = [[item] for item in target_ids]
        calls = []

        def storyboard_handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(request.content)
            self.assertEqual(
                payload["reasoning"]["effort"],
                OPENROUTER_REASONING_EFFORT,
            )
            self.assertEqual(
                payload["max_completion_tokens"],
                STORYBOARD_MAX_COMPLETION_TOKENS,
            )
            prompt = payload["messages"][1]["content"]
            self.assertIn("first_frame_prompt", prompt)
            self.assertIn("motion_prompt", prompt)
            self.assertIn("前 2 秒", prompt)
            self.assertIn("前 5 秒", prompt)
            self.assertIn("不能先给空镜", prompt)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "shots": [
                        {
                            "segment_id": segment_id,
                            "visual_intent": f"意图 {index}",
                            "first_frame_prompt": f"首帧 {index}",
                            "motion_prompt": f"动作 {index}",
                        }
                        for index, segment_id in enumerate(target_ids, 1)
                    ],
                }, ensure_ascii=False)}}],
            })

        storyboard_provider = OpenRouterStoryboardProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/storyboard",
            transport=httpx.MockTransport(storyboard_handler),
        )
        plan = await storyboard_provider.generate(
            script,
            "统一编辑插画风格",
            beat_groups,
            ["video", "image", "image", "video", "video"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [item.shot_id for item in plan.shots],
            [f"shot_{index:02d}" for index in range(1, 6)],
        )
        self.assertEqual(
            [item.narration_excerpt for item in plan.shots],
            [item.text for item in script.narration_segments],
        )

    async def test_storyboard_normalizes_a_missing_shot_without_second_call(self):
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-storyboard-repair",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        target_ids = [item.id for item in script.narration_segments]
        beat_groups = [[item] for item in target_ids]
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(request.content)
            prompt = payload["messages"][1]["content"]
            for segment_id in target_ids:
                self.assertIn(f'"segment_id": "{segment_id}"', prompt)
            returned_ids = [
                target_ids[0], target_ids[1], target_ids[3], target_ids[4]
            ]
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "shots": [
                        {
                            "segment_id": segment_id,
                            "visual_intent": f"模型意图 {segment_id}",
                            "first_frame_prompt": f"模型首帧 {segment_id}",
                            "motion_prompt": f"模型动作 {segment_id}",
                        }
                        for segment_id in returned_ids
                    ],
                }, ensure_ascii=False)}}],
            })

        provider = OpenRouterStoryboardProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api",
            model="test/storyboard",
            transport=httpx.MockTransport(handler),
        )
        plan = await provider.generate(
            script,
            "统一编辑插画风格",
            beat_groups,
            ["video", "image", "image", "video", "video"],
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(plan.shots), 5)
        self.assertEqual(
            [item.segment_id for item in plan.shots], target_ids
        )
        self.assertEqual(
            [item.visual_type for item in plan.shots],
            ["video", "image", "image", "video", "video"],
        )
        self.assertIn("心理机制", plan.shots[2].visual_intent)
        self.assertTrue(plan.shots[2].first_frame_prompt)
        self.assertTrue(plan.shots[2].motion_prompt)
        self.assertEqual(
            plan.shots[3].visual_intent,
            f"模型意图 {target_ids[3]}",
        )

    async def test_tts_stream_decoder_accepts_audio_and_terminal_event(self):
        encoded = base64.b64encode(b"mp3-bytes").decode("ascii")
        audio, duration, done = VolcengineTtsProvider._decode_line(json.dumps({
            "code": 0,
            "data": encoded,
            "addition": {"duration": "1250"},
        }))
        self.assertEqual(audio, b"mp3-bytes")
        self.assertEqual(duration, 1.25)
        self.assertFalse(done)
        _, _, done = VolcengineTtsProvider._decode_line(
            '{"code":20000000,"message":"done"}'
        )
        self.assertTrue(done)

        with self.assertRaises(ProviderUnavailable):
            VolcengineTtsProvider._decode_line(
                '{"code":0,"sequence":"not-a-number"}'
            )

    async def test_tts_synthesizes_normal_script_once_and_keeps_one_audio_asset(self):
        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="zh_female_vv_uranus_bigtts",
            api_key="speech-key",
        )
        card = SourceCard(
            **valid_card().model_dump(mode="json"),
            id="card-tts",
            revision=1,
            status="verified",
        )
        script = await TemplateScriptProvider().generate(card)
        synthesized_texts: list[str] = []

        async def synthesize_segment(text: str, path: Path, **options) -> float:
            synthesized_texts.append(text)
            self.assertEqual(
                options["voice_id"],
                "zh_female_vv_uranus_bigtts",
            )
            self.assertEqual(options["speed_ratio"], 1.0)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"full-audio")
            return 48.0

        provider._synthesize_segment = synthesize_segment
        with tempfile.TemporaryDirectory() as directory:
            manifest, generated = await provider.synthesize(script, Path(directory))

        self.assertEqual(len(synthesized_texts), 1)
        synthesized_without_spacing = "".join(synthesized_texts[0].split())
        self.assertTrue(all(
            "".join(
                VolcengineTtsProvider._prepare_text(item.narration).split()
            ) in synthesized_without_spacing
            for item in script.beats
        ))
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0].asset_id, "narration_full")
        self.assertEqual(
            {item.asset_id for item in manifest.segments},
            {"narration_full"},
        )
        self.assertEqual(manifest.segments[0].start_seconds, 0.0)
        self.assertAlmostEqual(
            manifest.segments[-1].start_seconds
            + manifest.segments[-1].duration_seconds,
            manifest.total_duration_seconds,
            places=3,
        )
        for left, right in zip(manifest.segments, manifest.segments[1:]):
            self.assertAlmostEqual(
                left.start_seconds + left.duration_seconds,
                right.start_seconds,
                places=3,
            )

    async def test_tts_segment_uses_v3_api_key_headers_and_ndjson(self):
        requests = []
        encoded = base64.b64encode(b"fake-mp3").decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(
                body["req_params"]["speaker"],
                "zh_male_ruyayichen_saturn_bigtts",
            )
            self.assertEqual(
                body["req_params"]["audio_params"]["speech_rate"],
                20,
            )
            return httpx.Response(200, text=(
                json.dumps({"code": 0, "data": encoded})
                + "\n"
                + json.dumps({"code": 20000000, "message": "done"})
                + "\n"
            ))

        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="zh_female_vv_uranus_bigtts",
            api_key="speech-key",
            transport=httpx.MockTransport(handler),
        )

        async def fixed_duration(path: Path) -> float:
            self.assertEqual(path.read_bytes(), b"fake-mp3")
            return 1.5

        provider._probe_duration = fixed_duration
        usage_records: list[ProviderUsageRecord] = []

        async def record_usage(usage: ProviderUsageRecord) -> None:
            usage_records.append(usage)

        with tempfile.TemporaryDirectory() as directory:
            duration = await provider._synthesize_segment(
                "这是一句真实接口契约测试。",
                Path(directory) / "voice.mp3",
                voice_id="zh_male_ruyayichen_saturn_bigtts",
                speed_ratio=1.2,
                on_usage=record_usage,
            )
        self.assertEqual(duration, 1.5)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["x-api-key"], "speech-key")
        self.assertEqual(
            requests[0].headers["x-api-resource-id"], "seed-tts-2.0"
        )
        self.assertEqual(len(usage_records), 1)
        self.assertTrue(usage_records[0].succeeded)
        self.assertEqual(usage_records[0].operation, "tts_synthesis")
        self.assertEqual(usage_records[0].quantity, len("这是一句真实接口契约测试。"))
        self.assertIn("语速 1.2x", usage_records[0].note)

    async def test_tts_preview_uses_selected_settings_and_separate_cost_stage(self):
        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="zh_female_vv_uranus_bigtts",
            api_key="speech-key",
        )
        captured = {}

        async def synthesize_segment(text, path, **options):
            captured.update(options)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preview-audio")
            return 2.5

        provider._synthesize_segment = synthesize_segment
        with tempfile.TemporaryDirectory() as directory:
            generated = await provider.synthesize_preview(
                "这是当前脚本的开场。",
                Path(directory),
                voice_id="zh_female_santongyongns_saturn_bigtts",
                speed_ratio=1.1,
            )
            self.assertEqual(generated.path.read_bytes(), b"preview-audio")
        self.assertEqual(
            captured["voice_id"],
            "zh_female_santongyongns_saturn_bigtts",
        )
        self.assertEqual(captured["speed_ratio"], 1.1)
        self.assertEqual(captured["operation"], "tts_preview")
        self.assertFalse(captured["probe_duration"])

    async def test_tts_legacy_credentials_use_app_key_header(self):
        provider = VolcengineTtsProvider(
            endpoint="https://openspeech.bytedance.com/api/v3/tts/unidirectional",
            resource_id="seed-tts-2.0",
            voice_id="voice-test",
            app_id="legacy-app-id",
            access_token="legacy-access-token",
        )
        headers = provider._headers("request-id")
        self.assertEqual(headers["X-Api-App-Key"], "legacy-app-id")
        self.assertEqual(headers["X-Api-Access-Key"], "legacy-access-token")
        self.assertNotIn("X-Api-Key", headers)


class QijiaVideoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = InMemoryAggregateRepository()
        self.storage = LocalArtifactStorage(self.root / "storage")
        self.service = QijiaVideoService(
            repository=self.repository,
            script_provider=TemplateScriptProvider(),
            storyboard_provider=TemplateStoryboardProvider(),
            image_provider=MockImageProvider(),
            tts_provider=SilentTtsProvider(),
            video_provider=MockVideoProvider(),
            renderer=FakeRenderer(),
            storage=self.storage,
            quality_checker=PassingQualityChecker(),
            media_packager=FakeMediaPackager(),
            work_root=self.root / "work",
        )
        self.actor = Actor(user_id=7, username="editor", role="member")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_person_research_enriches_the_job_without_adding_a_gate(self):
        brief = PersonResearchBrief.model_validate({
            "person_name": "阿尔弗雷德·阿德勒",
            "viewpoint": "真正影响孩子的，是孩子如何理解自己在家庭中的位置。",
            "summary": "用家庭位置感解释孩子如何参与真实任务。",
            "core_tension": "家长的快速帮助可能同时减少孩子的参与空间。",
            "audience_relevance": ["孩子卡住时，家长容易直接接手。"],
            "content_angles": ["把帮助拆成观察、提示和接手三个层级。"],
            "interaction_opportunity": "你最容易在哪类任务里直接接手？",
            "evidence": [{
                "claim": "有来源支持的人物与主题背景事实。",
                "source_title": "大学研究资料",
                "source_url": "https://example.edu/adler",
            }],
            "uncertainties": ["用户观点不是人物逐字引语。"],
            "model_id": "test/research-model",
            "prompt_version": PERSON_RESEARCH_PROMPT_VERSION,
            "generated_at": timestamp(),
        })

        class ResearchingScriptProvider(TemplateScriptProvider):
            def __init__(self):
                self.research_calls = 0
                self.generated_card: SourceCard | None = None
                self.generated_prompt = ""

            async def research_person_viewpoint(self, card, *, on_usage=None):
                del card, on_usage
                self.research_calls += 1
                return brief

            async def generate(self, card, prompt=None):
                self.generated_card = card.model_copy(deep=True)
                self.generated_prompt = str(prompt or "")
                return await super().generate(card, prompt)

        provider = ResearchingScriptProvider()
        self.service.script_provider = provider
        card = await self.service.create_source_card(
            PersonViewpointInput(
                person_name=brief.person_name,
                viewpoint=brief.viewpoint,
            ).to_source_card_input(),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        generated = await self.service.generate_script(job.id, self.actor)

        self.assertEqual(generated.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(provider.research_calls, 1)
        self.assertEqual(generated.research_brief, brief)
        self.assertEqual(generated.research_warning, "")
        self.assertIn("【自动研究简报】", provider.generated_prompt)
        self.assertIn(brief.content_angles[0], provider.generated_prompt)
        enriched = SourceCard.model_validate(generated.source_card_snapshot)
        self.assertEqual(
            [item.id for item in enriched.sources if item.id.startswith("research_source")],
            ["research_source_01"],
        )
        self.assertEqual(
            [item.text for item in enriched.verified_facts if item.id.startswith("research_fact")],
            [brief.evidence[0].claim],
        )
        boundaries = [item.text for item in enriched.interpretation_boundary]
        self.assertNotIn(
            "只围绕用户输入的观点展开，不补造人物经历、逐字引语、研究数据或来源出处。",
            boundaries,
        )
        self.assertTrue(any("有来源支持的事实" in item for item in boundaries))
        enriched_again = self.service._card_with_person_research(enriched, brief)
        self.assertEqual(len(enriched_again.sources), len(enriched.sources))
        self.assertEqual(
            len(enriched_again.verified_facts), len(enriched.verified_facts)
        )

    async def test_person_research_failure_continues_and_is_not_retried(self):
        class OneTimeScriptFailureProvider(TemplateScriptProvider):
            def __init__(self):
                self.research_calls = 0
                self.generate_calls = 0

            async def research_person_viewpoint(self, card, *, on_usage=None):
                del card, on_usage
                self.research_calls += 1
                raise ProviderUnavailable("研究服务暂时不可用")

            async def generate(self, card, prompt=None):
                self.generate_calls += 1
                if self.generate_calls == 1:
                    raise ProviderUnavailable("脚本服务暂时不可用")
                return await super().generate(card, prompt)

        provider = OneTimeScriptFailureProvider()
        self.service.script_provider = provider
        card = await self.service.create_source_card(
            PersonViewpointInput(
                person_name="阿尔弗雷德·阿德勒",
                viewpoint="真正影响孩子的，是孩子如何理解自己在家庭中的位置。",
            ).to_source_card_input(),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        with self.assertRaisesRegex(ProviderUnavailable, "脚本服务暂时不可用"):
            await self.service.generate_script(job.id, self.actor)
        failed = await self.service.get_job(job.id, self.actor)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertIn("已使用原始人物观点继续生成", failed.research_warning)

        completed = await self.service.generate_script(job.id, self.actor)

        self.assertEqual(completed.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(provider.research_calls, 1)
        self.assertEqual(provider.generate_calls, 2)
        self.assertIn("已使用原始人物观点继续生成", completed.research_warning)

    async def test_seedance_cost_keeps_submission_price_snapshot(self):
        job = VideoJob.model_validate({
            "id": "job-cost-snapshot",
            "state": "producing",
            "source_card_id": "card-cost",
            "source_card_revision": 1,
            "source_card_snapshot": {"title": "成本快照"},
        })
        queued = ProviderTask(
            provider="volcengine-seedance",
            provider_task_id="seedance-task-cost",
            request_fingerprint="c" * 64,
            request_id="shot_01",
            state="queued",
        )
        self.service._record_video_task_usage(job, queued)
        self.assertEqual(queued.pricing_rate_cny_per_million, 8)
        self.assertIsNone(queued.estimated_cost_cny)

        succeeded = queued.model_copy(update={
            "state": ProviderTaskState.SUCCEEDED,
            "usage_total_tokens": 100000,
        })
        self.service.seedance_price_per_million_tokens = 99
        self.service._record_video_task_usage(job, succeeded, queued)

        self.assertEqual(succeeded.pricing_rate_cny_per_million, 8)
        self.assertAlmostEqual(succeeded.estimated_cost_cny, 0.8)
        matching = [
            item for item in job.usage_records
            if item.request_id == "seedance-task-cost"
        ]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(matching[0].estimated_cost, 0.8)

    async def test_six_script_beats_are_all_grouped_into_five_visual_shots(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(
            card.id, self.actor, GenerationSettings(image_count=2)
        )
        job = await self.service.generate_script(job.id, self.actor)
        extra = job.script.beats[1].model_copy(deep=True)
        extra.id = "n01b"
        extra.role = "suspense"
        extra.narration = "真正值得追问的是，父母立刻帮忙时，孩子失去了什么？"
        extra.visual_direction = "家长的手停在任务上方，孩子抬头看向家长。"
        job.script.beats.insert(1, extra)

        groups = self.service._storyboard_beat_groups(job)

        self.assertEqual([len(group) for group in groups], [2, 1, 1, 1, 1])
        self.assertEqual(
            [beat.id for group in groups for beat in group],
            [beat.id for beat in job.script.beats],
        )

    async def test_ten_image_visual_plan_allocates_inside_script_beats(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(
            card.id, self.actor, GenerationSettings(image_count=10)
        )
        job = await self.service.generate_script(job.id, self.actor)

        groups = self.service._storyboard_beat_groups(job)
        visual_types = self.service._storyboard_visual_types(job, groups)
        flat_ids = [beat.id for group in groups for beat in group]
        compressed_ids = [
            beat_id
            for index, beat_id in enumerate(flat_ids)
            if index == 0 or beat_id != flat_ids[index - 1]
        ]

        self.assertEqual(len(groups), 13)
        self.assertEqual(compressed_ids, [beat.id for beat in job.script.beats])
        self.assertEqual(visual_types.count("video"), 3)
        self.assertEqual(visual_types.count("image"), 10)
        self.assertEqual(visual_types[0], "video")
        self.assertEqual(
            sum(
                beat.id == job.script.beats[0].id
                for group in groups
                for beat in group
            ),
            1,
        )

    async def test_true_overlong_narration_is_rejected_without_hidden_rewrite(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        script = job.script.model_copy(deep=True)
        for segment in script.beats:
            segment.narration = "长" * 130

        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 650 字"):
            await self.service.update_script(
                job.id,
                script,
                job.revision,
                self.actor,
            )

        unchanged = await self.service.get_job(job.id, self.actor)
        self.assertEqual(unchanged.script_hash, job.script_hash)

    async def test_approval_never_rewrites_the_reviewed_script(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        legacy_script = job.script.model_copy(deep=True)
        for index, segment in enumerate(legacy_script.narration_segments):
            segment.text = "长" * (148 if index < 4 else 147)
        legacy_script.hook = legacy_script.narration_segments[0].text
        legacy_script.closing = legacy_script.narration_segments[-1].text
        job.script = legacy_script
        job.script_hash = content_hash(legacy_script)
        job.script_review = await self.service.script_provider.review(
            card, legacy_script
        )
        job = await self.service._save_job(job, self.actor)

        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 739 字"):
            await self.service.approve_script(
                job.id,
                job.revision,
                job.script_hash,
                self.actor,
            )

    async def test_generated_script_target_length_is_advisory(self):
        class UnderlongScriptProvider(TemplateScriptProvider):
            async def generate(self, card, prompt=None):
                script = await super().generate(card, prompt)
                for segment in script.narration_segments:
                    segment.text = "短" * 10
                script.hook = script.narration_segments[0].text
                script.closing = script.narration_segments[-1].text
                return script

        self.service.script_provider = UnderlongScriptProvider()
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)

        generated = await self.service.generate_script(job.id, self.actor)
        self.assertEqual(generated.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(narration_char_count(generated.script.narration_text()), 50)

    async def test_overlong_tts_stops_before_visual_cost_and_can_reopen_script(self):
        class OverlongTtsProvider(SilentTtsProvider):
            name = "overlong-tts-test"

            async def synthesize(
                self,
                script,
                workspace,
                *,
                voice_id=None,
                speed_ratio=1.0,
            ):
                manifest, files = await super().synthesize(
                    script,
                    workspace,
                    voice_id=voice_id,
                    speed_ratio=speed_ratio,
                )
                manifest.provider = self.name
                manifest.total_duration_seconds = 146.582
                return manifest, files

        image_provider = RecordingImageProvider()
        self.service.tts_provider = OverlongTtsProvider()
        self.service.image_provider = image_provider
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )

        with self.assertRaisesRegex(
            QualityGateFailed, "narration_duration_range=146.582"
        ):
            await self.service.produce(job.id, self.actor)

        failed = await self.service.get_job(job.id, self.actor)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.failed_stage, "production")
        self.assertEqual(image_provider.prompts, [])
        self.assertEqual(failed.first_frame_candidates, [])
        reopened = await self.service.reopen_script_review(
            failed.id,
            failed.revision,
            self.actor,
        )
        self.assertEqual(reopened.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertIsNone(reopened.narration_manifest)
        self.assertIsNone(reopened.render_manifest)
        self.assertEqual(reopened.approvals, [])

    async def test_overlong_script_failed_in_production_returns_to_editor(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        legacy_script = job.script.model_copy(deep=True)
        for index, segment in enumerate(legacy_script.narration_segments):
            segment.text = "长" * (148 if index < 4 else 147)
        legacy_script.hook = legacy_script.narration_segments[0].text
        legacy_script.closing = legacy_script.narration_segments[-1].text
        job.script = legacy_script
        job.script_hash = content_hash(legacy_script)
        job = await self.service._save_job(job, self.actor)
        self.assertEqual(job.state, JobState.SCRIPT_APPROVED)
        self.assertTrue(self.service.needs_script_revision(job))

        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 739 字"):
            await self.service.produce(job.id, self.actor)

        failed = await self.service.get_job(job.id, self.actor)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.failed_stage, "production")
        self.assertTrue(self.service.needs_script_revision(failed))
        reopened = await self.service.reopen_script_review(
            failed.id,
            failed.revision,
            self.actor,
        )
        self.assertEqual(reopened.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(reopened.failed_stage, "")
        self.assertEqual(reopened.error, "")
        self.assertEqual(reopened.approvals, [])
        self.assertEqual(
            narration_char_count(reopened.script.narration_text()), 739
        )
        with self.assertRaisesRegex(QualityGateFailed, "纯旁白共 739 字"):
            await self.service.approve_script(
                reopened.id,
                reopened.revision,
                reopened.script_hash,
                self.actor,
            )

    async def test_duration_recovery_reuses_already_paid_visuals(self):
        image_provider = RecordingImageProvider()
        self.service.image_provider = image_provider
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        original_script = job.script.model_copy(deep=True)
        image_call_count = len(image_provider.prompts)
        video_call_count = len(self.service.video_provider._requests)
        request_fingerprints = [
            item.fingerprint() for item in job.visual_requests
        ]

        long_script = job.script.model_copy(deep=True)
        for index, segment in enumerate(long_script.narration_segments):
            segment.text = "长" * (148 if index < 4 else 147)
        long_script.hook = long_script.narration_segments[0].text
        long_script.closing = long_script.narration_segments[-1].text
        job.script = long_script
        job.script_hash = content_hash(long_script)
        job.script_review = await self.service.script_provider.review(
            card, long_script
        )
        job = await self.service._save_job(job, self.actor)

        failed = await self.service.mark_execution_failed(
            job.id,
            "quality",
            "成片自动质检未通过：duration_range=146.582",
            self.actor,
        )
        reopened = await self.service.reopen_script_review(
            failed.id,
            failed.revision,
            self.actor,
        )
        self.assertIsNotNone(reopened.storyboard_plan)
        self.assertIsNotNone(reopened.render_manifest)
        self.assertEqual(len(reopened.visual_requests), 3)
        self.assertEqual(
            narration_char_count(reopened.script.narration_text()), 739
        )
        reopened = await self.service.update_script(
            reopened.id,
            original_script,
            reopened.revision,
            self.actor,
        )
        self.assertEqual(
            [item.fingerprint() for item in reopened.visual_requests],
            request_fingerprints,
        )
        self.assertEqual(
            reopened.storyboard_plan.input_hash,
            self.service._storyboard_input_hash(reopened),
        )
        approved = await self.service.approve_script(
            reopened.id,
            reopened.revision,
            reopened.script_hash,
            self.actor,
        )
        completed = await self.service.produce(approved.id, self.actor)

        self.assertEqual(completed.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(len(image_provider.prompts), image_call_count)
        self.assertEqual(
            len(self.service.video_provider._requests), video_call_count
        )

    async def test_reference_image_upload_is_private_and_size_bounded(self):
        upload = UploadFile(
            filename="style.png",
            file=io.BytesIO(MockImageProvider._PNG),
        )
        with patch.object(qijia_api.runtime, "storage", self.storage):
            asset = await qijia_api._store_reference_image(upload)
        self.assertEqual(asset.media_type, "image/png")
        self.assertLessEqual(asset.size_bytes, qijia_api.MAX_REFERENCE_IMAGE_BYTES)
        self.assertTrue(asset.object_key.startswith("qijia-video/reference-images/"))
        self.assertNotIn("style.png", asset.object_key)
        self.assertTrue(self.storage.path_for(asset.object_key).is_file())

    async def test_global_reference_image_reaches_all_seedream_requests(self):
        class RecordingStoryboardProvider(TemplateStoryboardProvider):
            base_style = ""

            async def generate(
                self, script, base_style, beat_groups, visual_types
            ):
                self.base_style = base_style
                return await super().generate(
                    script, base_style, beat_groups, visual_types
                )

        reference_path = self.root / "reference.png"
        reference_path.write_bytes(MockImageProvider._PNG)
        reference_asset = await self.storage.put_file(
            object_key="qijia-video/reference-images/test.png",
            path=reference_path,
            asset_id="reference_image_test",
            media_type="image/png",
        )
        provider = RecordingImageProvider()
        storyboard_provider = RecordingStoryboardProvider()
        self.service.image_provider = provider
        self.service.storyboard_provider = storyboard_provider
        card = await self.service.create_source_card(
            valid_card(reference_assets=[reference_asset.model_dump(mode="json")]),
            self.actor,
        )
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        conflicting_style = "写实摄影，霓虹紫色灯光，真人电影质感。"
        job = await self.service.create_job(
            card.id,
            self.actor,
            GenerationSettings(
                seedance_prompt=conflicting_style,
                image_count=10,
            ),
        )
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)

        self.assertEqual(len(provider.reference_image_urls), 13)
        self.assertEqual(
            set(provider.reference_image_urls),
            {"local://qijia-video/reference-images/test.png"},
        )
        self.assertIn("参考图是画风", storyboard_provider.base_style)
        self.assertNotIn(conflicting_style, storyboard_provider.base_style)
        self.assertTrue(all("【视觉基准】" in prompt for prompt in provider.prompts))
        self.assertTrue(all(conflicting_style not in prompt for prompt in provider.prompts))
        self.assertTrue(all(
            "【视觉基准】" in request.prompt
            and conflicting_style not in request.prompt
            for request in job.visual_requests
        ))

    async def test_two_distinct_approvals_create_complete_package(self):
        call_order: list[str] = []

        class OrderedImageProvider(MockImageProvider):
            async def generate(
                self,
                prompt: str,
                *,
                seed: int,
                reference_image_url: str = "",
            ):
                call_order.append("image")
                return await super().generate(
                    prompt,
                    seed=seed,
                    reference_image_url=reference_image_url,
                )

        class OrderedVideoProvider(MockVideoProvider):
            def __init__(self):
                super().__init__()
                self._refreshing_status = False

            async def submit(self, request, *, first_frame_url=""):
                if not self._refreshing_status:
                    call_order.append("video_submit")
                return await super().submit(
                    request,
                    first_frame_url=first_frame_url,
                )

            async def get_status(
                self, provider_task_id, request_fingerprint
            ):
                self._refreshing_status = True
                try:
                    return await super().get_status(
                        provider_task_id, request_fingerprint
                    )
                finally:
                    self._refreshing_status = False

        self.service.image_provider = OrderedImageProvider()
        self.service.video_provider = OrderedVideoProvider()
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        self.assertEqual(job.state, JobState.SCRIPT_REVIEW_REQUIRED)
        self.assertEqual(job.script_review.input_hash, job.script_hash)

        with self.assertRaises(RevisionConflict):
            await self.service.approve_script(
                job.id, job.revision, "0" * 64, self.actor
            )
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        self.assertEqual(job.approval("script").actor, "editor")

        seed_widths: list[int] = []
        with patch(
            "qijia_video.service.secrets.randbits",
            side_effect=lambda bits: seed_widths.append(bits) or len(seed_widths),
        ):
            job = await self.service.produce(job.id, self.actor)
        self.assertEqual(seed_widths, [31] * 13)
        self.assertEqual(
            call_order,
            ["image"] * 3 + ["video_submit"] * 3 + ["image"] * 10,
        )
        self.assertEqual(job.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(len(job.review_bundle_hash), 64)
        self.assertTrue(job.render_manifest.subtitle_cues)
        self.assertEqual(
            [cue.text for cue in job.render_manifest.screen_text_cues],
            ["陪伴，不是接管"],
        )
        self.assertNotIn(
            "陪伴，不是接管",
            "".join(item.text for item in job.narration_manifest.segments),
        )
        self.assertEqual(
            (job.render_manifest.width, job.render_manifest.height),
            (1080, 1920),
        )
        self.assertTrue(all(
            len(cue.text) <= 20 for cue in job.render_manifest.subtitle_cues
        ))
        self.assertTrue(all(
            cue.text == cue.text.strip() for cue in job.render_manifest.subtitle_cues
        ))
        self.assertEqual(job.generation_settings.image_count, 10)
        self.assertEqual(job.generation_settings.shot_count, 13)
        self.assertEqual(len(job.visual_requests), 3)
        self.assertTrue(all(
            request.resolution == "1080p"
            and 8 <= request.duration_seconds <= 10
            for request in job.visual_requests
        ))
        video_shot_ids = [
            shot.shot_id
            for shot in job.storyboard_plan.shots
            if shot.visual_type == "video"
        ]
        self.assertEqual(
            [request.request_id for request in job.visual_requests],
            video_shot_ids,
        )
        self.assertIn("【抖音开场执行】", job.visual_requests[0].prompt)
        self.assertTrue(all(
            "【抖音开场执行】" not in request.prompt
            for request in job.visual_requests[1:]
        ))
        self.assertEqual(len(job.video_tasks), 3)
        self.assertEqual(len(job.visual_versions), 3)
        self.assertTrue(all(
            version.version == 1
            and version.asset is not None
            and version.task.state == ProviderTaskState.SUCCEEDED
            for version in job.visual_versions
        ))
        self.assertEqual(
            [task.request_id for task in job.video_tasks],
            [request.request_id for request in job.visual_requests],
        )
        self.assertTrue(all(
            task.state == ProviderTaskState.SUCCEEDED for task in job.video_tasks
        ))
        self.assertIsNotNone(job.storyboard_plan)
        self.assertEqual(len(job.storyboard_plan.shots), 13)
        planned_beat_ids = [
            beat_id
            for shot in job.storyboard_plan.shots
            for beat_id in shot.beat_ids
        ]
        self.assertEqual(
            [
                beat_id
                for index, beat_id in enumerate(planned_beat_ids)
                if index == 0 or beat_id != planned_beat_ids[index - 1]
            ],
            [beat.id for beat in job.script.beats],
        )
        self.assertEqual(
            [shot.visual_type for shot in job.storyboard_plan.shots].count("video"),
            3,
        )
        self.assertEqual(
            [shot.visual_type for shot in job.storyboard_plan.shots].count("image"),
            10,
        )
        self.assertEqual(job.storyboard_plan.shots[0].visual_type, "video")
        self.assertEqual(len(job.first_frame_candidates), 13)
        self.assertTrue(all(
            0 <= candidate.seed <= SEEDREAM_MAX_SEED
            for candidate in job.first_frame_candidates
        ))
        self.assertEqual(job.frame_selections, [])
        self.assertTrue(all(
            candidate.asset is not None
            for candidate in job.first_frame_candidates
        ))
        self.assertTrue(all(
            shot.selected_candidate_id == f"frame_{shot.shot_id}_01"
            for shot in job.storyboard_plan.shots
        ))
        self.assertEqual(len(job.render_manifest.visual_blocks), 13)
        self.assertEqual(
            sum(
                block.duration_in_frames
                for block in job.render_manifest.visual_blocks
            ),
            job.render_manifest.duration_in_frames,
        )
        public_payload = qijia_api.public_job_payload(job, {
            "id": 7,
            "username": "editor",
            "role": "member",
            "permissions": ["qijia_video"],
        })
        self.assertEqual(public_payload["script"]["schema_version"], "2.0")
        self.assertIn("beats", public_payload["script"])
        self.assertNotIn("narration_segments", public_payload["script"])
        self.assertTrue(all(
            "source_url" not in candidate
            for candidate in public_payload["first_frame_candidates"]
        ))
        selected_assets = {
            candidate.asset.asset_id
            for candidate in job.first_frame_candidates
            if candidate.asset
            and any(
                shot.selected_candidate_id == candidate.candidate_id
                for shot in job.storyboard_plan.shots
            )
        }
        self.assertEqual(len(selected_assets), 13)
        self.assertEqual(
            {request.first_frame_asset_id for request in job.visual_requests},
            {
                candidate.asset.asset_id
                for candidate in job.first_frame_candidates
                if candidate.asset
                and candidate.shot_id in set(video_shot_ids)
                and any(
                    shot.selected_candidate_id == candidate.candidate_id
                    for shot in job.storyboard_plan.shots
                )
            },
        )
        self.assertTrue(all(
            "【首帧驱动】" in request.prompt
            and "【动作与运镜】" in request.prompt
            and "不得新增任何文字" in request.prompt
            for request in job.visual_requests
        ))
        self.assertTrue(all(
            segment.text not in request.prompt
            for segment in job.script.narration_segments
            for request in job.visual_requests
        ))
        blocks = job.render_manifest.visual_blocks
        self.assertEqual(len(blocks), 13)
        self.assertEqual(
            [block.type for block in blocks],
            [
                "generated_video"
                if shot.visual_type == "video"
                else "generated_image"
                for shot in job.storyboard_plan.shots
            ],
        )
        self.assertTrue(all(
            block.playback_rate == 1.0
            for block in blocks
            if block.type == "generated_video"
        ))
        self.assertTrue(all(
            block.asset_id
            and not block.headline
            and not block.body
            for block in blocks
        ))
        self.assertEqual(
            [block.shot_id for block in blocks],
            [f"shot_{index:02d}" for index in range(1, 14)],
        )
        self.assertEqual(job.render_manifest.video_title, job.script.video_title)
        self.assertEqual(job.render_manifest.cover_text, job.script.cover_text)
        self.assertIn(job.render_manifest.cover_asset_id, selected_assets)
        self.assertEqual(blocks[0].start_frame, 0)
        self.assertEqual(
            blocks[-1].start_frame + blocks[-1].duration_in_frames,
            job.render_manifest.duration_in_frames,
        )
        self.assertEqual(
            sum(block.duration_in_frames for block in blocks),
            job.render_manifest.duration_in_frames,
        )
        self.assertTrue(all(
            left.start_frame + left.duration_in_frames == right.start_frame
            for left, right in zip(blocks, blocks[1:])
        ))
        with self.assertRaises(RevisionConflict):
            await self.service.approve_final(
                job.id, job.revision, "0" * 64, self.actor
            )
        job = await self.service.approve_final(
            job.id, job.revision, job.review_bundle_hash, self.actor
        )
        self.assertEqual(job.state, JobState.PACKAGED)
        self.assertEqual(
            [item.kind for item in job.approvals], ["script", "final"]
        )
        names = {item.name for item in job.artifacts}
        self.assertTrue(REQUIRED_PACKAGE_NAMES.issubset(names))
        draft = next(item for item in job.artifacts if item.name == "draft.mp4")
        final = next(item for item in job.artifacts if item.name == "final.mp4")
        self.assertEqual(draft.asset.sha256, final.asset.sha256)
        self.assertEqual(draft.asset.asset_id, final.asset.asset_id)
        self.assertEqual(draft.asset.object_key, final.asset.object_key)
        archive = self.root / "release.zip"
        await self.service.build_release_archive(job.id, self.actor, archive)
        expected_final = self.root / "expected-final.mp4"
        await self.storage.materialize(final.asset, expected_final)
        with zipfile.ZipFile(archive) as release:
            self.assertEqual(set(release.namelist()), REQUIRED_PACKAGE_NAMES)
            self.assertEqual(release.read("final.mp4"), expected_final.read_bytes())

    async def test_one_shot_can_regenerate_and_switch_versions_without_rerunning_others(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)

        shot_id = job.visual_requests[1].request_id
        original_requests = {
            item.request_id: item for item in job.visual_requests
        }
        original = original_requests[shot_id]
        original_version = next(
            item for item in job.visual_versions if item.shot_id == shot_id
        )
        frame_candidates = [
            item for item in job.first_frame_candidates if item.shot_id == shot_id
        ]
        self.assertEqual(len(frame_candidates), 1)
        frame_candidate = frame_candidates[0]
        events = []
        job = await self.service.regenerate_shot(
            job.id,
            shot_id,
            "2.5D 编辑插画动画，一扇门缓慢打开，暖色光线进入房间。",
            original.fingerprint(),
            self.actor,
            progress=events.append,
            seedance_model=SEEDANCE_FLAGSHIP_MODEL,
        )

        self.assertEqual(job.state, JobState.FINAL_REVIEW_REQUIRED)
        versions = sorted(
            (item for item in job.visual_versions if item.shot_id == shot_id),
            key=lambda item: item.version,
        )
        self.assertEqual([item.version for item in versions], [1, 2])
        self.assertEqual(versions[0].version_id, original_version.version_id)
        self.assertIsNotNone(versions[0].asset)
        self.assertIsNotNone(versions[1].asset)
        self.assertEqual(versions[1].request.prompt, (
            "2.5D 编辑插画动画，一扇门缓慢打开，暖色光线进入房间。"
        ))
        self.assertIsNotNone(versions[1].request.seed)
        self.assertEqual(versions[0].request.model_id, SEEDANCE_EFFICIENT_MODEL)
        self.assertEqual(versions[1].request.model_id, SEEDANCE_FLAGSHIP_MODEL)
        selected = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )
        self.assertEqual(selected.fingerprint(), versions[1].request.fingerprint())
        self.assertEqual(
            selected.first_frame_asset_id, frame_candidate.asset.asset_id
        )
        storyboard_shot = next(
            item for item in job.storyboard_plan.shots if item.shot_id == shot_id
        )
        self.assertEqual(
            storyboard_shot.selected_candidate_id,
            frame_candidate.candidate_id,
        )
        self.assertEqual(len(self.service._all_video_tasks(job)), 4)
        self.assertEqual(
            {
                item.request_id: item.fingerprint()
                for item in job.visual_requests
                if item.request_id != shot_id
            },
            {
                item.request_id: item.fingerprint()
                for item in original_requests.values()
                if item.request_id != shot_id
            },
        )
        self.assertTrue(any(
            event.get("workflow") == "shot_edit" for event in events
        ))
        self.assertEqual(
            [item.kind for item in job.approvals], ["script"]
        )

        job = await self.service.select_shot_version(
            job.id,
            shot_id,
            versions[0].version_id,
            selected.fingerprint(),
            self.actor,
        )
        restored = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )
        self.assertEqual(restored.fingerprint(), original.fingerprint())
        restored_storyboard_shot = next(
            item for item in job.storyboard_plan.shots if item.shot_id == shot_id
        )
        original_frame = next(
            item
            for item in frame_candidates
            if item.asset.asset_id == original.first_frame_asset_id
        )
        self.assertEqual(
            restored_storyboard_shot.selected_candidate_id,
            original_frame.candidate_id,
        )
        self.assertEqual(len([
            item for item in job.visual_versions if item.shot_id == shot_id
        ]), 2)
        self.assertEqual(len(self.service._all_video_tasks(job)), 4)

    async def test_failed_shot_regeneration_keeps_the_reviewable_draft(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        original_bundle = job.review_bundle_hash
        shot_id = job.visual_requests[1].request_id
        original = next(
            item for item in job.visual_requests if item.request_id == shot_id
        )

        class FailingVideoProvider:
            name = "failing-video"

            async def submit(self, request, *, first_frame_url=""):
                del request, first_frame_url
                raise ProviderUnavailable("测试中的单镜头失败")

        self.service.video_provider = FailingVideoProvider()
        with self.assertRaisesRegex(ProviderUnavailable, "单镜头失败"):
            await self.service.regenerate_shot(
                job.id,
                shot_id,
                "新的镜头提示词",
                original.fingerprint(),
                self.actor,
            )
        latest = await self.service.get_job(job.id, self.actor)
        self.assertEqual(latest.state, JobState.FINAL_REVIEW_REQUIRED)
        self.assertEqual(latest.review_bundle_hash, original_bundle)
        current = next(
            item for item in latest.visual_requests
            if item.request_id == shot_id
        )
        self.assertEqual(current.fingerprint(), original.fingerprint())
        self.assertIn("原成片仍然保留", latest.error)

    async def test_frontend_generation_settings_flow_into_both_providers(self):
        class RecordingScriptProvider(TemplateScriptProvider):
            prompt = None

            async def generate(self, card, prompt=None):
                self.prompt = prompt
                return await super().generate(card, prompt)

        provider = RecordingScriptProvider()
        self.service.script_provider = provider
        class RecordingStoryboardProvider(TemplateStoryboardProvider):
            base_style = None

            async def generate(
                self, script, base_style, beat_groups, visual_types
            ):
                self.base_style = base_style
                return await super().generate(
                    script, base_style, beat_groups, visual_types
                )

        storyboard_provider = RecordingStoryboardProvider()
        self.service.storyboard_provider = storyboard_provider
        class RecordingTtsProvider(SilentTtsProvider):
            voice_id = None
            speed_ratio = None

            async def synthesize(
                self,
                script,
                workspace,
                *,
                voice_id=None,
                speed_ratio=1.0,
            ):
                self.voice_id = voice_id
                self.speed_ratio = speed_ratio
                return await super().synthesize(
                    script,
                    workspace,
                    voice_id=voice_id,
                    speed_ratio=speed_ratio,
                )

        tts_provider = RecordingTtsProvider()
        self.service.tts_provider = tts_provider
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        settings = GenerationSettings(
            script_prompt="用更像真实对话的方式写脚本。",
            seedance_prompt="低饱和纪录片风格，固定机位。",
            video_resolution="1080p",
            tts_voice_id="zh_male_ruyayichen_saturn_bigtts",
            tts_speed_ratio=1.1,
        )
        job = await self.service.create_job(
            card.id, self.actor, settings
        )
        job = await self.service.generate_script(job.id, self.actor)
        self.assertTrue(provider.prompt.startswith(settings.script_prompt))
        self.assertIn("语速 1.1x", provider.prompt)
        self.assertIn("245-325 个汉字", provider.prompt)

        job = await self.service.update_script(
            job.id,
            job.script,
            job.revision,
            self.actor,
            seedance_prompt="自然窗光，手持纪录片风格。",
        )
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(job.id, self.actor)
        self.assertEqual(
            tts_provider.voice_id,
            "zh_male_ruyayichen_saturn_bigtts",
        )
        self.assertEqual(tts_provider.speed_ratio, 1.1)
        self.assertEqual(job.narration_manifest.speed_ratio, 1.1)
        self.assertEqual(
            storyboard_provider.base_style,
            "自然窗光，手持纪录片风格。",
        )
        requests = job.visual_requests
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(
            request.resolution == "1080p"
            and request.model_id == SEEDANCE_EFFICIENT_MODEL
            and 8 <= request.duration_seconds <= 10
            for request in requests
        ))
        self.assertEqual(
            (job.render_manifest.width, job.render_manifest.height),
            (1080, 1920),
        )
        self.assertTrue(all(
            request.prompt.startswith("自然窗光，手持纪录片风格。")
            for request in requests
        ))
        legacy = requests[0].model_copy(update={
            "resolution": "720p",
            "duration_seconds": 5,
        })
        job.visual_requests = [legacy]
        self.assertEqual(self.service._build_visual_requests(job), [legacy])

    async def test_progress_events_and_background_package_are_resumable(self):
        events = []
        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(
            job.id, self.actor, progress=events.append
        )
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        job = await self.service.produce(
            job.id, self.actor, progress=events.append
        )
        job = await self.service.approve_final(
            job.id,
            job.revision,
            job.review_bundle_hash,
            self.actor,
            package_immediately=False,
        )
        self.assertEqual(job.state, JobState.FINAL_APPROVED)
        job = await self.service.package(
            job.id, self.actor, progress=events.append
        )
        self.assertEqual(job.state, JobState.PACKAGED)

        stages = {event["stage"] for event in events}
        self.assertTrue({
            "material_confirmed",
            "script_generation",
            "confirm_script",
            "tts",
            "storyboard",
            "first_frames",
            "seedance_shot_1",
            "seedance_shot_2",
            "seedance_shot_3",
            "seedance_parallel",
            "visual_assets",
            "remotion_render",
            "remotion_normalize",
            "artifact_upload",
            "quality",
            "confirm_final",
            "package",
        }.issubset(stages))
        self.assertNotIn("frame_selection", stages)

    async def test_legacy_frozen_video_requests_resume_without_seedream_cost(self):
        class ForbiddenImageProvider:
            name = "forbidden-image"

            async def generate(self, prompt, *, seed):
                del prompt, seed
                raise AssertionError("legacy task must not submit Seedream")

            async def download(self, source_url, destination):
                del source_url, destination
                raise AssertionError("legacy task has no Seedream asset")

        card = await self.service.create_source_card(valid_card(), self.actor)
        card = await self.service.verify_source_card(
            card.id, card.revision, self.actor
        )
        job = await self.service.create_job(card.id, self.actor)
        job = await self.service.generate_script(job.id, self.actor)
        job = await self.service.approve_script(
            job.id, job.revision, job.script_hash, self.actor
        )
        legacy_requests = [
            item.model_copy(update={
                "resolution": "720p",
                "duration_seconds": 5,
            })
            for item in self.service._build_visual_requests(job)
        ]
        job.visual_requests = legacy_requests
        job = await self.service._save_job(job, self.actor)
        self.service.image_provider = ForbiddenImageProvider()
        job = await self.service.produce(job.id, self.actor)
        self.assertEqual(job.visual_requests, legacy_requests)
        self.assertIsNone(job.storyboard_plan)
        self.assertEqual(job.first_frame_candidates, [])

    async def test_high_risk_card_fails_closed(self):
        card = await self.service.create_source_card(
            valid_card(risk_level="high"), self.actor
        )
        with self.assertRaises(QualityGateFailed):
            await self.service.verify_source_card(
                card.id, card.revision, self.actor
            )

    async def test_team_can_read_but_only_owner_can_mutate_resources(self):
        card = await self.service.create_source_card(valid_card(), self.actor)
        stranger = Actor(user_id=8, username="stranger", role="member")
        with self.assertRaises(AccessDenied):
            await self.service.get_source_card(card.id, stranger)
        with self.assertRaises(AccessDenied):
            await self.service.update_source_card(
                card.id, valid_card(), card.revision, stranger
            )
        shared = await self.service.view_source_card(card.id, stranger)
        self.assertEqual(shared.id, card.id)
        self.assertEqual(
            [item.id for item in await self.service.list_source_cards(stranger)],
            [card.id],
        )


class QijiaVideoPermissionTests(unittest.TestCase):
    @staticmethod
    def client_for(user: dict) -> TestClient:
        app = FastAPI()
        app.include_router(qijia_api.api_router)
        app.include_router(qijia_api.page_router)
        app.dependency_overrides[auth_service.get_current_user] = lambda: user
        return TestClient(app)

    def test_api_and_page_require_independent_permission(self):
        denied = self.client_for({
            "id": 2,
            "username": "member",
            "role": "member",
            "permissions": ["video"],
        })
        self.assertEqual(denied.get("/api/qijia-video/capabilities").status_code, 403)
        self.assertEqual(denied.get("/qijia-video").status_code, 403)

        allowed = self.client_for({
            "id": 3,
            "username": "editor",
            "role": "member",
            "permissions": ["qijia_video"],
        })
        response = allowed.get("/api/qijia-video/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["module"], "qijia_video")
        self.assertNotIn("mock", response.json()["data"]["script_provider"])
        self.assertNotIn("mock", response.json()["data"]["tts_provider"])
        self.assertNotIn("mock", response.json()["data"]["video_provider"])
        self.assertEqual(
            response.json()["data"]["generation_defaults"]["image_count"], 10
        )
        self.assertEqual(
            response.json()["data"]["generation_defaults"]["shot_count"], 13
        )
        self.assertEqual(
            response.json()["data"]["generation_defaults"]["video_resolution"],
            "1080p",
        )
        self.assertEqual(
            response.json()["data"]["generation_defaults"]["seedance_model"],
            SEEDANCE_EFFICIENT_MODEL,
        )
        tts_pricing = response.json()["data"]["tts_pricing"]
        self.assertEqual(
            [item["id"] for item in tts_pricing["voices"]],
            [
                "zh_female_vv_uranus_bigtts",
                "zh_female_santongyongns_saturn_bigtts",
                "zh_male_ruyayichen_saturn_bigtts",
            ],
        )
        self.assertEqual(
            [item["ratio"] for item in tts_pricing["speed_ratios"]],
            [1.0, 1.1, 1.2],
        )
        self.assertEqual(
            [item["speech_rate"] for item in tts_pricing["speed_ratios"]],
            [0, 10, 20],
        )
        self.assertEqual(tts_pricing["default_speed_ratio"], 1.2)
        self.assertEqual(tts_pricing["preview_max_characters"], 60)
        self.assertEqual(tts_pricing["preview_max_estimated_cost_cny"], 0.03)
        self.assertTrue(tts_pricing["preview_cost_confirmation_required"])
        self.assertEqual(
            response.json()["data"]["seedance_pricing"]["yuan_per_million_tokens"],
            4.2,
        )
        self.assertEqual(
            [
                item["yuan_per_million_tokens"]
                for item in response.json()["data"]["seedance_pricing"]["models"]
            ],
            [4.2, 46.0],
        )
        self.assertEqual(
            response.json()["data"]["seedream_pricing"]["candidates_per_shot"],
            1,
        )
        self.assertEqual(
            response.json()["data"]["douyin_performance"][
                "estimated_cny_per_success"
            ],
            0.0134,
        )
        self.assertEqual(
            response.json()["data"]["douyin_performance"][
                "short_link_estimated_cny"
            ],
            0.0201,
        )
        self.assertEqual(
            response.json()["data"]["douyin_performance"][
                "requests_per_refresh"
            ],
            1,
        )
        self.assertNotIn("frame_evaluator", response.json()["data"])
        self.assertEqual(allowed.get("/qijia-video").status_code, 200)


if __name__ == "__main__":
    unittest.main()
