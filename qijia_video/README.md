# 模块边界

`qijia_video` 是独立短视频生产领域模块，核心分层如下：

- `contracts.py`：来源卡、脚本、分镜、生成请求、资产和发布包协议；
- `service.py`：状态机与完整生产链路；
- `ports.py`：Provider、存储、渲染和仓储端口；
- `infrastructure/`：OpenRouter、火山引擎、TOS、PostgreSQL、FFmpeg 与 Remotion 适配器；
- `run_service.py`：后台任务进度、互斥和部署重启恢复；
- `auth.py`：单管理员安全 Cookie 登录；
- `api.py`：独立 API 与页面路由；
- `web/`：不依赖前端框架的工作台页面。

Remotion 位于仓库根目录 `video_renderer/`，只读取 `render_manifest.json` 与本地化素材，不访问数据库，也不调用生成模型。

完整部署、配置、测试和数据边界见仓库根目录 `README.md`。
