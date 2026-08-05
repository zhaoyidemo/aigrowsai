"""确定性媒体质检。"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from qijia_video.contracts import QualityReport, RenderManifest, timestamp
from qijia_video.errors import ProviderUnavailable


class FfprobeQualityChecker:
    name = "ffprobe"

    async def _run(self, *args: str, timeout: int = 300) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, "命令执行超时"
        return process.returncode, output.decode("utf-8", errors="replace")

    async def inspect(
        self, path: Path, manifest: RenderManifest
    ) -> QualityReport:
        ffprobe = shutil.which("ffprobe")
        ffmpeg = shutil.which("ffmpeg")
        if not ffprobe or not ffmpeg:
            raise ProviderUnavailable("缺少 FFmpeg/ffprobe")
        code, output = await self._run(
            ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate:format=duration",
            "-of", "json",
            str(path),
        )
        if code != 0:
            return QualityReport(
                automatic_status="failed",
                checks=[{"id": "ffprobe", "passed": False, "detail": output[-1000:]}],
                generated_at=timestamp(),
            )
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return QualityReport(
                automatic_status="failed",
                checks=[{"id": "ffprobe_json", "passed": False, "detail": "无法解析 ffprobe 输出"}],
                generated_at=timestamp(),
            )
        streams = list(data.get("streams") or [])
        video = next((row for row in streams if row.get("codec_type") == "video"), {})
        audio = next((row for row in streams if row.get("codec_type") == "audio"), {})
        duration = float((data.get("format") or {}).get("duration") or 0)
        expected_duration = manifest.duration_in_frames / manifest.fps
        frame_rate = str(video.get("r_frame_rate") or "")

        def rate_value(value: str) -> float:
            try:
                numerator, denominator = value.split("/", 1)
                return float(numerator) / float(denominator)
            except (ValueError, ZeroDivisionError):
                return 0.0

        def is_faststart(media_path: Path) -> bool:
            atom_types: list[bytes] = []
            try:
                size = media_path.stat().st_size
                with media_path.open("rb") as handle:
                    offset = 0
                    while offset + 8 <= size and len(atom_types) < 128:
                        handle.seek(offset)
                        header = handle.read(8)
                        if len(header) != 8:
                            break
                        atom_size = int.from_bytes(header[:4], "big")
                        atom_type = header[4:8]
                        header_size = 8
                        if atom_size == 1:
                            extended = handle.read(8)
                            if len(extended) != 8:
                                break
                            atom_size = int.from_bytes(extended, "big")
                            header_size = 16
                        elif atom_size == 0:
                            atom_size = size - offset
                        if atom_size < header_size:
                            break
                        atom_types.append(atom_type)
                        offset += atom_size
            except OSError:
                return False
            return (
                b"moov" in atom_types
                and b"mdat" in atom_types
                and atom_types.index(b"moov") < atom_types.index(b"mdat")
            )

        checks = [
            {"id": "video_stream", "passed": bool(video), "detail": video.get("codec_name", "")},
            {"id": "audio_stream", "passed": bool(audio), "detail": audio.get("codec_name", "")},
            {
                "id": "resolution",
                "passed": (
                    video.get("width") == manifest.width
                    and video.get("height") == manifest.height
                ),
                "detail": {
                    "actual": f"{video.get('width', 0)}x{video.get('height', 0)}",
                    "expected": f"{manifest.width}x{manifest.height}",
                },
            },
            {"id": "video_codec", "passed": video.get("codec_name") == "h264", "detail": video.get("codec_name", "")},
            {"id": "pixel_format", "passed": video.get("pix_fmt") == "yuv420p", "detail": video.get("pix_fmt", "")},
            {"id": "frame_rate", "passed": abs(rate_value(frame_rate) - 30.0) < 0.01, "detail": frame_rate},
            {"id": "audio_codec", "passed": audio.get("codec_name") == "aac", "detail": audio.get("codec_name", "")},
            {"id": "sample_rate", "passed": str(audio.get("sample_rate") or "") == "48000", "detail": str(audio.get("sample_rate") or "")},
            {"id": "duration", "passed": abs(duration - expected_duration) <= 0.5,
             "detail": {"actual": round(duration, 3), "expected": round(expected_duration, 3)}},
            {"id": "duration_range", "passed": 45.0 <= duration <= 75.0,
             "detail": round(duration, 3)},
            {"id": "faststart", "passed": is_faststart(path), "detail": "moov before mdat"},
            {"id": "ai_label_manifest", "passed": manifest.ai_content_label.enabled, "detail": "manifest"},
            {"id": "brand_overlay_disabled", "passed": manifest.brand_overlay is None, "detail": "null"},
        ]
        decode_code, decode_output = await self._run(
            ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-",
            timeout=600,
        )
        checks.append({
            "id": "full_decode",
            "passed": decode_code == 0,
            "detail": decode_output[-1000:] if decode_code else "ok",
        })
        passed = all(bool(item.get("passed")) for item in checks)
        return QualityReport(
            automatic_status="review_ready" if passed else "failed",
            checks=checks,
            warnings=[] if passed else ["存在未通过的确定性媒体检查"],
            generated_at=timestamp(),
        )
