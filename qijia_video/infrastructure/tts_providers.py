"""豆包语音合成 2.0 Provider。"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from qijia_video.contracts import (
    NarrationAudioSegment,
    NarrationManifest,
    ProviderUsageRecord,
    ScriptDraft,
    timestamp,
)
from qijia_video.errors import ProviderUnavailable
from qijia_video.ports import GeneratedFile


# The online V3 endpoint documents a 1024-byte UTF-8 ceiling. Keep a small
# margin for provider-side normalization while allowing a normal 220-300
# Chinese-character script to remain one request.
TTS_TEXT_MAX_BYTES = 1000
# Kept for import compatibility with older tests/integrations. New narration
# synthesis does not insert artificial gaps between approved script beats.
SEGMENT_GAP_SECONDS = 0.18
UsageRecorder = Callable[[ProviderUsageRecord], Awaitable[None]]


class VolcengineTtsProvider:
    """使用 V3 HTTP 单向流式接口生成真实旁白。"""

    name = "volcengine-seed-tts-2.0"

    def __init__(
        self,
        *,
        endpoint: str,
        resource_id: str,
        voice_id: str,
        api_key: str = "",
        app_id: str = "",
        access_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 180.0,
    ):
        self.endpoint = str(endpoint or "").strip()
        self.resource_id = str(resource_id or "").strip()
        self.voice_id = str(voice_id or "").strip()
        self.api_key = str(api_key or "").strip()
        self.app_id = str(app_id or "").strip()
        self.access_token = str(access_token or "").strip()
        self.transport = transport
        self.timeout_seconds = max(30.0, float(timeout_seconds))

    @property
    def configured(self) -> bool:
        credentials_ready = bool(self.api_key) or bool(
            self.app_id and self.access_token
        )
        return bool(
            credentials_ready
            and self.endpoint.startswith("https://")
            and self.resource_id
            and self.voice_id
        )

    def _headers(self, request_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            # 旧版控制台把 App ID 放在名为 App-Key 的协议头中。
            headers["X-Api-App-Key"] = self.app_id
            headers["X-Api-Access-Key"] = self.access_token
        return headers

    @staticmethod
    def _decode_line(line: str) -> tuple[bytes, float | None, bool]:
        value = str(line or "").strip()
        if not value or value.startswith(("event:", ":")):
            return b"", None, False
        if value.startswith("data:"):
            value = value[5:].strip()
        if not value or value == "[DONE]":
            return b"", None, True
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("豆包 TTS 返回了无法解析的数据流") from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable("豆包 TTS 返回格式无效")
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = -1
        if code not in (0, 3000, 20000000):
            message = str(payload.get("message") or "未知错误")
            raise ProviderUnavailable(f"豆包 TTS 合成失败（{code}）：{message[:500]}")
        raw_audio = payload.get("data") or ""
        audio = b""
        if raw_audio:
            try:
                audio = base64.b64decode(str(raw_audio), validate=True)
            except (ValueError, TypeError) as exc:
                raise ProviderUnavailable("豆包 TTS 返回的音频数据无效") from exc
        addition = payload.get("addition")
        duration = None
        if isinstance(addition, dict) and addition.get("duration") is not None:
            try:
                duration = max(0.0, float(addition["duration"]) / 1000.0)
            except (TypeError, ValueError):
                duration = None
        try:
            sequence = int(payload.get("sequence") or 0)
        except (TypeError, ValueError) as exc:
            raise ProviderUnavailable("豆包 TTS 返回的 sequence 无效") from exc
        done = code == 20000000 or sequence < 0
        return audio, duration, done

    async def _record_usage(
        self,
        recorder: UsageRecorder | None,
        *,
        request_id: str,
        text: str,
        succeeded: bool,
        note: str = "",
    ) -> None:
        if not recorder:
            return
        usage = ProviderUsageRecord(
            usage_id=f"usage_tts_{request_id.replace('-', '')}",
            operation="tts_synthesis",
            provider=self.name,
            model_id=self.resource_id,
            request_id=request_id,
            succeeded=succeeded,
            quantity=len(text),
            unit="character",
            note=note,
            occurred_at=timestamp(),
        )
        try:
            await recorder(usage)
        except Exception as exc:
            raise ProviderUnavailable(
                "TTS 调用已经发生，但成本账本无法持久化；流程已停止"
            ) from exc

    async def _synthesize_segment(
        self,
        text: str,
        destination: Path,
        *,
        on_usage: UsageRecorder | None = None,
    ) -> float:
        if len(text.encode("utf-8")) > TTS_TEXT_MAX_BYTES:
            raise ProviderUnavailable(
                "单次豆包 TTS 文本超过 1000 UTF-8 字节"
            )
        request_id = str(uuid.uuid4())
        body = {
            "user": {"uid": "qijia-video"},
            "req_params": {
                "text": text,
                "speaker": self.voice_id,
                "audio_params": {"format": "mp3", "sample_rate": 24000},
            },
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        chunks: list[bytes] = []
        reported_duration = 0.0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=20.0),
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    headers=self._headers(request_id),
                    json=body,
                ) as response:
                    if response.status_code >= 400:
                        content = (await response.aread()).decode(
                            "utf-8", errors="replace"
                        )
                        request_log_id = response.headers.get("x-tt-logid", "")
                        suffix = (
                            f"；logid={request_log_id}" if request_log_id else ""
                        )
                        raise ProviderUnavailable(
                            f"豆包 TTS 返回 HTTP {response.status_code}："
                            f"{content[:500]}{suffix}"
                        )
                    async for line in response.aiter_lines():
                        audio, duration, _ = self._decode_line(line)
                        if audio:
                            chunks.append(audio)
                        if duration:
                            reported_duration = max(reported_duration, duration)
            if not chunks:
                raise ProviderUnavailable("豆包 TTS 没有返回音频")
        except ProviderUnavailable:
            await self._record_usage(
                on_usage,
                request_id=request_id,
                text=text,
                succeeded=False,
                note="TTS 请求失败，是否计费需与供应商账单核对",
            )
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            await self._record_usage(
                on_usage,
                request_id=request_id,
                text=text,
                succeeded=False,
                note="TTS 网络异常，是否计费需与供应商账单核对",
            )
            raise ProviderUnavailable("豆包 TTS 请求失败") from exc
        # A non-empty audio response is the billable provider outcome. Record
        # it before local ffprobe/concatenation can fail so paid work is not
        # mislabeled as a zero-cost provider failure.
        await self._record_usage(
            on_usage,
            request_id=request_id,
            text=text,
            succeeded=True,
        )
        destination.write_bytes(b"".join(chunks))
        actual_duration = await self._probe_duration(destination)
        return actual_duration or reported_duration

    @staticmethod
    def _prepare_text(text: str) -> str:
        """Keep the approved wording while giving TTS a clean sentence boundary."""
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if value and not value.endswith(("。", "！", "？", "…", ".", "!", "?")):
            value += "。"
        return value

    @staticmethod
    def _split_utf8_text(text: str, max_bytes: int) -> list[str]:
        """Split only when one natural sentence exceeds the provider ceiling."""

        pieces: list[str] = []
        current = ""
        for character in str(text or ""):
            candidate = current + character
            if current and len(candidate.encode("utf-8")) > max_bytes:
                pieces.append(current)
                current = character
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces

    @classmethod
    def _synthesis_chunks(cls, texts: list[str]) -> list[str]:
        """Use one request normally and the minimum number required by the API."""

        natural_units: list[str] = []
        for text in texts:
            sentences = [
                item.strip()
                for item in re.findall(r".+?(?:[。！？!?；;]+|$)", text)
                if item.strip()
            ]
            for sentence in sentences or [text]:
                if len(sentence.encode("utf-8")) <= TTS_TEXT_MAX_BYTES:
                    natural_units.append(sentence)
                else:
                    natural_units.extend(
                        cls._split_utf8_text(sentence, TTS_TEXT_MAX_BYTES)
                    )

        chunks: list[str] = []
        current = ""
        for unit in natural_units:
            candidate = f"{current}\n{unit}" if current else unit
            if current and len(candidate.encode("utf-8")) > TTS_TEXT_MAX_BYTES:
                chunks.append(current)
                current = unit
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _timing_weight(text: str) -> float:
        """Estimate relative spoken time without adding another alignment API."""

        weight = 0.0
        for character in str(text or ""):
            if character.isspace():
                continue
            if character in "，、,:：":
                weight += 0.35
            elif character in "。！？!?；;…":
                weight += 0.7
            else:
                weight += 1.0
        return max(1.0, weight)

    @classmethod
    def _estimate_segment_timeline(
        cls,
        script: ScriptDraft,
        spoken_texts: list[str],
        total_duration: float,
    ) -> list[NarrationAudioSegment]:
        weights = [cls._timing_weight(text) for text in spoken_texts]
        total_weight = sum(weights)
        cursor = 0.0
        segments: list[NarrationAudioSegment] = []
        cumulative_weight = 0.0
        for index, (beat, text, weight) in enumerate(
            zip(script.beats, spoken_texts, weights)
        ):
            cumulative_weight += weight
            end = (
                total_duration
                if index == len(spoken_texts) - 1
                else round(total_duration * cumulative_weight / total_weight, 3)
            )
            end = min(total_duration, max(cursor + 0.001, end))
            segments.append(NarrationAudioSegment(
                segment_id=beat.id,
                text=text,
                # Every timing row points at the one canonical narration asset.
                asset_id="narration_full",
                start_seconds=round(cursor, 3),
                duration_seconds=round(end - cursor, 3),
            ))
            cursor = end
        return segments

    @staticmethod
    async def _run(*args: str, timeout: int = 300) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderUnavailable("旁白音频处理超时") from exc
        log = output.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ProviderUnavailable("旁白音频处理失败：" + log[-3000:])
        return log

    @classmethod
    async def _probe_duration(cls, path: Path) -> float:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise ProviderUnavailable("真实 TTS 需要 FFmpeg/ffprobe")
        output = await cls._run(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            timeout=30,
        )
        try:
            return max(0.0, float(output.strip().splitlines()[-1]))
        except (ValueError, IndexError) as exc:
            raise ProviderUnavailable("无法读取豆包 TTS 音频时长") from exc

    @classmethod
    async def _concat_segments(
        cls,
        sources: list[Path],
        destination: Path,
        *,
        gap_seconds: float = SEGMENT_GAP_SECONDS,
    ) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ProviderUnavailable("真实 TTS 需要 FFmpeg")
        destination.parent.mkdir(parents=True, exist_ok=True)
        arguments: list[str] = [ffmpeg, "-y"]
        input_count = 0
        for index, source in enumerate(sources):
            arguments.extend(["-i", str(source)])
            input_count += 1
            if index < len(sources) - 1 and gap_seconds > 0:
                arguments.extend([
                    "-f",
                    "lavfi",
                    "-t",
                    f"{gap_seconds:.3f}",
                    "-i",
                    "anullsrc=r=48000:cl=mono",
                ])
                input_count += 1
        normalized = [
            (
                f"[{index}:a]aresample=48000,"
                f"aformat=sample_fmts=s16:channel_layouts=mono[a{index}]"
            )
            for index in range(input_count)
        ]
        inputs = "".join(f"[a{index}]" for index in range(input_count))
        filters = ";".join(normalized + [
            f"{inputs}concat=n={input_count}:v=0:a=1[out]"
        ])
        arguments.extend([
            "-filter_complex",
            filters,
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ])
        await cls._run(*arguments)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderUnavailable("未能生成完整旁白音轨")
        return destination

    async def synthesize(
        self, script: ScriptDraft, workspace: Path
    ) -> tuple[NarrationManifest, list[GeneratedFile]]:
        return await self.synthesize_with_usage(script, workspace)

    async def synthesize_with_usage(
        self,
        script: ScriptDraft,
        workspace: Path,
        *,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[NarrationManifest, list[GeneratedFile]]:
        if not self.configured:
            raise ProviderUnavailable(
                "真实 TTS 未配置：请设置 VOLCENGINE_TTS_API_KEY，或设置 "
                "VOLCENGINE_TTS_APP_ID 与 VOLCENGINE_TTS_ACCESS_TOKEN"
            )
        audio_dir = workspace / "audio"
        spoken_texts = [self._prepare_text(item.narration) for item in script.beats]
        chunks = self._synthesis_chunks(spoken_texts)
        if not chunks:
            raise ProviderUnavailable("完整口播稿没有可合成内容")

        chunk_paths: list[Path] = []
        chunk_durations: list[float] = []
        for index, text in enumerate(chunks, 1):
            path = audio_dir / (
                "narration.mp3" if len(chunks) == 1 else f"chunk_{index:02d}.mp3"
            )
            duration = (
                await self._synthesize_segment(
                    text,
                    path,
                    on_usage=on_usage,
                )
                if on_usage
                else await self._synthesize_segment(text, path)
            )
            if duration <= 0:
                raise ProviderUnavailable("完整旁白音频时长无效")
            chunk_paths.append(path)
            chunk_durations.append(duration)

        if len(chunk_paths) == 1:
            full_path = chunk_paths[0]
            total_duration = round(chunk_durations[0], 3)
            media_type = "audio/mpeg"
            sample_rate = 24000
        else:
            # This is only the API-limit fallback for unusually long manual
            # scripts. Temporary chunks are never uploaded or rendered.
            full_path = await self._concat_segments(
                chunk_paths,
                audio_dir / "narration.wav",
                gap_seconds=0.0,
            )
            total_duration = round(await self._probe_duration(full_path), 3)
            media_type = "audio/wav"
            sample_rate = 48000

        segments = self._estimate_segment_timeline(
            script, spoken_texts, total_duration
        )
        generated = [GeneratedFile(
            asset_id="narration_full",
            path=full_path,
            media_type=media_type,
            duration_seconds=total_duration,
        )]
        return NarrationManifest(
            provider=self.name,
            voice_id=self.voice_id,
            sample_rate=sample_rate,
            total_duration_seconds=total_duration,
            full_audio_asset_id="narration_full",
            segments=segments,
        ), generated
