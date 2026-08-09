"""FFmpeg 输出规范化与发布封面适配器。"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from qijia_video.errors import ProviderUnavailable


class FfmpegMediaPackager:
    name = "ffmpeg"

    @staticmethod
    def available() -> tuple[bool, str]:
        path = shutil.which("ffmpeg")
        return (bool(path), path or "未找到 FFmpeg")

    async def _run(self, *args: str, timeout: int = 600) -> str:
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
            raise ProviderUnavailable("FFmpeg 处理超时") from exc
        log = output.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ProviderUnavailable("FFmpeg 处理失败：" + log[-3000:])
        return log

    async def _probe_duration(self, source: Path) -> float:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise ProviderUnavailable("未找到 ffprobe")
        output = await self._run(
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source),
            timeout=60,
        )
        try:
            duration = float(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ProviderUnavailable("无法读取 Seedance 视频实际时长") from exc
        if duration <= 0:
            raise ProviderUnavailable("Seedance 视频实际时长无效")
        return duration

    async def prepare_video_for_timeline(
        self,
        source: Path,
        destination: Path,
        *,
        minimum_duration_seconds: float,
    ) -> tuple[Path, float]:
        """Probe provider output and freeze only its final frame when needed."""

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ProviderUnavailable("未找到 FFmpeg")
        actual_duration = await self._probe_duration(source)
        minimum = max(0.001, float(minimum_duration_seconds))
        if actual_duration + (1 / 30) >= minimum:
            return source, actual_duration

        # Add one frame of margin so a rounded 30 fps Remotion chapter never
        # seeks beyond the prepared media. The moving portion remains 1x;
        # only the final provider frame is held.
        target_duration = minimum + (1 / 30)
        padding = max(0.0, target_duration - actual_duration)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            ffmpeg,
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-vf", f"tpad=stop_mode=clone:stop_duration={padding:.6f}",
            "-an",
            "-r", "30",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-t", f"{target_duration:.6f}",
            str(destination),
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderUnavailable("未能准备 Seedance 时间轴视频")
        prepared_duration = await self._probe_duration(destination)
        if prepared_duration + (1 / 30) < minimum:
            raise ProviderUnavailable("Seedance 时间轴视频仍短于对应旁白章节")
        return destination, prepared_duration

    async def normalize(self, source: Path, destination: Path) -> Path:
        ready, detail = self.available()
        if not ready:
            raise ProviderUnavailable(detail)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0",
            # Keep the already-rendered video bit-for-bit while giving every
            # narration a stable short-form listening level.
            "-c:v", "copy",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=7",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-movflags", "+faststart",
            str(destination),
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderUnavailable("FFmpeg 未生成规范化成片")
        return destination

    async def extract_cover(
        self, source: Path, destination: Path, *, at_seconds: float = 1.0
    ) -> Path:
        ready, detail = self.available()
        if not ready:
            raise ProviderUnavailable(detail)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-ss", f"{max(0.0, float(at_seconds)):.3f}",
            "-i", str(source),
            "-frames:v", "1",
            "-q:v", "2",
            str(destination),
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderUnavailable("FFmpeg 未生成封面")
        return destination
