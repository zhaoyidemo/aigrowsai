# 齐家 AI 短视频供应商 Spike 报告

> 本报告记录拆仓前的真实链路验证证据。独立仓库保留同一 Provider 与渲染实现，但新的 PostgreSQL、鉴权和 Railway 部署仍需按根目录 README 完成一次真实端到端验收。

> 状态：Seedance 2.0 真实端到端链路已跑通；同一生产 Key 已确认可解析默认 1.0 Pro Fast，480P / 720P / 1080P 可选规格仍需分别做生产验收
> 更新日期：2026-08-06
> 范围：Seedance 1.0 Pro Fast / 1.5 Pro 历史兼容 / 2.0、TTS、Remotion、FFmpeg、存储与恢复

> 2026-08-12 测试策略变更：按产品决定，新任务暂时改用 1.5 Pro 控制测试成本。具体型号只由代码内 `model_registry.py` 管理，Railway 不再保存型号；前端读取后端实际装配结果且不自动回退。下方 2026-08-06 的 404 诊断原样保留，因此部署后必须先用当前生产 Key 重新验证 1.5 Pro 可提交，再开展付费批量测试。

## 结论

真实 OpenRouter 脚本、豆包 TTS 2.0、Seedance 2.0、TOS 与 Remotion 已在 Railway 跑通同一生产状态机；Web 运行时不注入 Mock。账号鉴权、模型开通、TTS 2.0 权限和完整发布包链路已经得到真实任务验证。2026-08-06 使用 Railway 当前生产 `ARK_API_KEY` 做了不含 `content` 的零生成诊断：模型列表将 1.5 Pro 标记为 `Retiring`，其任务入口返回 404；1.0 Pro Fast 与 2.0 均先解析模型再返回预期的参数校验 400，证明 Key、项目、区域和入口正常。新任务因此默认使用 1.0 Pro Fast 无声模式，单镜头可升级 2.0；默认模型的真实成片质量仍需部署后验收，方舟 tokens 与费用展示沿用现有真实回传链路。

## 已确认

### Seedance 公共能力

- 火山方舟当前生产模型列表可解析 Seedance 1.0 Pro Fast、1.5 Pro 与 2.0，其中 1.5 Pro 已标记 `Retiring` 且拒绝新任务；
- 默认模型 ID：`doubao-seedance-1-0-pro-fast-251015`；按量刊例价为无声 `¥4.2/百万 tokens`，支持原生 1080P；
- 历史模型 ID：`doubao-seedance-1-5-pro-251215`；仅继续查询已取得 Provider Task ID 的历史任务，不再提交新任务；
- 单镜头升级模型 ID：`doubao-seedance-2-0-260128`；无视频输入按量刊例价为 `¥46/百万 tokens`；
- 任务接口采用异步提交、查询、下载语义；
- 已实现并接线独立 `VideoProvider` 和火山方舟 HTTP 适配器；
- 提交请求遇到连接超时等“服务端是否接单未知”的情况时不自动重提，避免重复扣费；
- 下载只接受 HTTPS、域名白名单、视频 MIME 和大小上限。

以上接口形态由齐家账号的真实任务和零生成协议诊断验证；额度、计费变化与对应区域仍以火山方舟控制台为准。

参考：

- https://developer.volcengine.com/articles/7628567056649125942
- https://api.volcengine.com/api-docs/view?action=CreateContentsGenerationsTasks&serviceCode=ark&version=2024-01-01

### Remotion / FFmpeg 本地链路

- Remotion 固定版本：`4.0.503`；
- Node：要求 `>=22`；
- Chrome Headless Shell 已通过 `remotion browser ensure` 安装；
- 渲染契约支持 480 × 854、720 × 1280、1080 × 1920 三档 30 fps 竖屏视频；现有本地实测证据仅覆盖 480 × 854；
- Remotion 在唯一一次编码中直接输出 H.264 / BT.709 limited-range `yuv420p` / AAC 48 kHz；
- FFmpeg 后处理只用 stream copy 整理 MP4 并写入 `faststart`，不再二次编码；
- ffprobe 检查视频流、音频流、分辨率、帧率、编码、像素格式、采样率、时长和 `faststart`；
- FFmpeg 对最终文件执行从头到尾完整解码；
- 自动质检失败会停止，不生成最终发布包。

本机 480×854 完整 CLI 链路最近一次实测：总墙钟时间约 163.8 秒，生成成片时长 48.043 秒，`final.mp4` 为 1,288,006 字节；像素格式、采样率、`faststart` 与完整解码等自动媒体检查全部通过。该数字只证明链路可执行，不可作为 Debian 生产 Worker 的容量规划数据。

### 发布包与人工确认

- 脚本确认和成片确认是两个独立动作、两条审批记录；
- 成片确认绑定视频、封面、字幕、文案和来源的组合哈希；
- `final.mp4` 与已确认 `draft.mp4` 字节一致；
- 发布时 `final.mp4` 直接引用已确认的不可变草稿资产，不重复下载和上传视频；
- 已生成完整产物清单和溯源信息；
- 视频模板不包含齐家 AI 品牌、产品介绍、CTA 或内嵌 AI 标识；发布者在抖音、视频号和小红书发布时自行完成平台要求的 AI 内容标注。

## 仍需生产观察

### Seedance 账号级 Spike

真实 `ARK_API_KEY` 与 Seedance 1.0 Pro Fast、2.0 权限已用 Railway 当前生产配置确认。仍需持续验证：

- 9:16、480P / 720P / 1080P、8-10 秒组合各自的实际画质和 tokens；
- 任务排队、状态枚举、审核拒绝码和取消语义；
- 临时下载 URL 域名与有效期；
- 真实生成时长、首帧可用率、重试率和费用；
- 进程重启后凭 Provider Task ID 继续查询且重复付费次数为 0；
- 无中文伪文字、无未授权真人、无品牌水印的提示词稳定性。

### 真实 TTS 账号级 Spike

已实现豆包 V3 HTTP 单向流式 Provider、固定 `seed-tts-2.0` 音色、整篇旁白单次合成和真实总时长读取，并已使用齐家账号跑通。只有文本超过单请求字节上限时才做最少次数的本地无间隔拼接，临时分块不上传、不进入渲染。仍需持续验证：

- 可商用固定中文音色及授权；
- 段级/句级时间戳；
- 人名、书名和术语发音控制；
- 单句重生成；
- 音频格式、响度、断句和真实费用。

### 目标生产环境

生产 Dockerfile 已加入 Node 22、Chromium、FFmpeg 与 Noto CJK 字体，并已在 Railway 完成单任务端到端验证；尚未完成容量与故障演练：

- 系统 Chromium、FFmpeg/ffprobe 和固定中文字体的实际运行；
- 60 秒视频的 CPU、内存、临时磁盘、P50/P95 渲染耗时；
- Worker 被终止后的恢复行为；
- TOS 私有对象存储和签名下载；
- 当前法律实体适用的 Remotion 许可档位及采购状态。

### Remotion 许可核对

已查阅 2026-07-31 可见的 Remotion 官方许可/价格页面：个人及不超过 3 人的公司可使用免费许可；4 人及以上的公司需要 Company License；自动化视频创建工具属于官方列出的 Automators 场景。齐家项目所属法律实体、团队人数和预计渲染量尚未提供，因此不能替用户判断最终许可档位，也不能把本地技术验证视为商业许可已经满足。

参考：https://www.remotion.dev/license

生产 Dockerfile 已按用户确认完成修改；Railway Secret 仍必须由用户在平台安全配置，仓库不会写入真实值。

## 下一次 Spike 的最小步骤

1. 部署本次低成本规格和用量展示；
2. 从工作台提交一条低风险测试选题并确认脚本；
3. 验证五条 9:16、480p、4 秒、无模型音频镜头只各提交一次，并记录每条 `usage.total_tokens`；
4. 对照火山方舟账单核对工作台费用估算，并确认 480×854 成片足以验证内容需求；
5. 人为重启 Worker，确认已有 Provider Task 只查询、不重提；
6. 记录真实耗时、错误码和 Railway 资源峰值；
7. 更新本报告后开放给内容团队使用。
