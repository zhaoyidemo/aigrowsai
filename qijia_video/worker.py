"""单任务 Worker 入口；生产模式把 Remotion/Chrome 与 Web 进程隔离。"""
from __future__ import annotations

import argparse
import asyncio

from qijia_video.runtime import run_worker_task


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m qijia_video.worker")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run_worker_task(args.task_id)))


if __name__ == "__main__":
    main()
