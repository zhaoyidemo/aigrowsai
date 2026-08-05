# 独立化记录

本仓库的短视频领域代码与 Remotion 渲染器提取自：

```text
repository: https://github.com/zhaoyidemo/zhuiwen
source commit: d43833026a1158906fcaf9fbbbf5d05323851cc0
migration date: 2026-08-05
```

独立化只改变部署边界：

- 新增单管理员 Cookie 鉴权；
- 新增专用 PostgreSQL `video_resources` 与 `video_runs`；
- 新增独立后台任务进度、互斥和重启恢复；
- 新增独立 `main.py`、Docker、Railway 和 Secrets 配置；
- 移除对原仓库 `services.auth_service`、`services.task_service` 和主站导航的依赖。

脚本、分镜、Seedream、Seedance、TTS、TOS、Remotion、人工确认和发布包领域协议保持不变。首次上线默认不复制旧 PostgreSQL 任务记录，也不移动 TOS 对象。
