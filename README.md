# retouch-travel-portrait

`v0.7.0-beta` 是一个面向 Codex/Agent 的自然旅行人像修图 Skill。它把修图拆成固定顺序、可回退、可审查的流程，而不是一次性套用“美颜滤镜”。默认路径完全在本地运行确定性图像操作，并在每个候选结果前停下来等待视觉确认。

它当前适合：单人旅行人像的整体明暗与白平衡修正、克制的肤色与肤质整理，以及三种轻量风格。它不适合：换脸、改变年龄、瘦脸瘦身、五官或身体塑形、重妆、背景替换、儿童照片、多位主要人物，或不经审查的大规模自动出图。

## 当前可以做到什么

- 固定八阶段：照片检查、光线、色彩、皮肤、五官检查、头发与服装检查、背景检查、最终审查。
- 三种模式：`natural`（默认自然）、`fresh`（清透）和 `warm-film`（暖调胶片）。风格只改变全图光线和色彩边界，不改变身份与几何规则。
- 默认零模型调用：使用 Pillow/NumPy 的本地确定性处理；每个真正改变像素的候选都必须显式接受或拒绝。
- 可选单模型候选：只在明确启用时使用不可变原图调用一次；请求、提示词、候选、proof 和审查结果以哈希绑定。
- 完整视觉证据：比较图、差异热图、带“肤色密度主焦点 + 上方中央回退焦点”的双组 100% 细节、720 像素缩略图、1080 像素手机预览，以及绑定这些文件的 manifest。
- 文件夹队列：每张图片建立独立 run，单图失败不会污染其他图片；队列不会自动接受候选。

这里的“完成”表示流程、证据和十项审查全部闭环，不等同于已经证明 90% 的真实世界审美成功率。

## 输入契约

公开 beta 只接受符合以下全部条件的源图：

- JPG/JPEG、PNG 或 WebP，扩展名必须与真实编码一致；
- 单帧；
- 8-bit `RGB` 或 8-bit 灰度 `L`；
- 按 EXIF 方向显示后的短边至少 768 像素；
- EXIF Orientation 为 1、3、6 或 8；
- 一位主要成年人。

透明/alpha、带透明信息的调色板图片、动画或多帧图片、CMYK、调色板/16-bit/其他像素模式、镜像 EXIF 方向 2/4/5/7、损坏或不匹配的格式会被拒绝。输入预检不会修改源文件：

```bash
python3 retouch-travel-portrait/scripts/input_preflight.py portrait.jpg
```

## 安装与触发

需要 Python 3.9+。安装运行依赖：

```bash
python3 -m pip install -r retouch-travel-portrait/requirements.txt
```

核心运行依赖刻意只包含 Pillow 和 NumPy。仓库发布校验脚本
`scripts/validate_repo.py` 还需要 `PyYAML>=6,<7`；它属于开发/发布依赖，未混入
Skill 的核心 `requirements.txt`。从发布仓库根目录运行校验前安装：

```bash
python3 -m pip install -r requirements-validation.txt
python3 scripts/validate_repo.py .
```

把整个 `retouch-travel-portrait` 文件夹放入 Agent 能发现的 Skill 目录中。例如，项目级 Codex 工作区可使用 `.agents/skills/retouch-travel-portrait`，个人安装可放入 Codex 的个人 skills 目录。保持文件夹名和 `SKILL.md` 中的 `name` 一致。

安装后可以直接向 Agent 提出：

> 使用 `$retouch-travel-portrait`，以 natural 模式自然地修饰这张旅行人像，并展示每个需要我确认的候选与最终十项审查。

Agent 必须读取 `SKILL.md` 并执行其中的审查门，不应把“脚本运行成功”当作图片已经合格。

## 单图最小流程

先编译计划并创建不可变 run：

```bash
python3 retouch-travel-portrait/scripts/compile_pipeline.py \
  --mode natural \
  --output plan.json

python3 retouch-travel-portrait/scripts/pipeline_state.py init portrait.jpg \
  --plan plan.json \
  --run-dir portrait-run
```

阶段 1–7 必须严格按顺序处理。应用阶段先生成一个待审候选：

```bash
python3 retouch-travel-portrait/scripts/execute_pipeline.py \
  --run-dir portrait-run \
  --stage light_balance
```

视觉比较后再明确接受或拒绝，并提供当前图片专属备注：

```bash
python3 retouch-travel-portrait/scripts/execute_pipeline.py \
  --run-dir portrait-run \
  --stage light_balance \
  --review-decision accept \
  --notes "面部与背景亮度仍协调，高光未溢出，原有夜景氛围保留"
```

检查型或无需修改的阶段使用 `--confirm-inspection` 和具体原因。完整命令与阶段边界见 `SKILL.md`。

最终阶段先生成 proof：

```bash
python3 retouch-travel-portrait/scripts/render_proof.py \
  portrait-run/original.jpg portrait-run/checkpoints/<current-file> \
  --output-dir portrait-run/proof
```

然后从模板创建仅当前用户可读写的审查文件；POSIX 系统可直接运行：

```bash
install -m 600 \
  retouch-travel-portrait/references/manual-review-template.json \
  portrait-run/manual-review.json
```

其他系统应使用等效权限限制，使该文件仅当前用户可访问。接着填写十项
`status` + 图像专属 `note`，并把审查绑定到当前 manifest。
`note` 不能只写“通过”“没问题”或重复检查名；规范化后至少 16 个字符，并且要
自然地写出该项实际看见的证据。无需猜代码关键词，按下面的观察对象写完整句子即可：

- 皮肤：毛孔、纹理或细纹是否保留；
- 脸与颈：两处肤色、底色或色调是否一致；
- 眼白与牙齿：是否可见，若可见是否出现蓝/青偏色；
- 嘴唇：颜色、饱和度和边缘是否自然；
- 背景线条：门框、墙线、地平线或其他几何线是否仍笔直；
- 头发：边缘、碎发、光晕或涂抹是否异常；
- 主体与背景：亮度、光线或曝光是否协调；
- 放大与缩略：100% 细节和缩略图是否同时协调；
- 相对原图：并排对比或差异热图中的变化是否过量；
- 手机预览：1080 手机画面中的亮度、高光和阴影是否舒适。

例如：`100%细节中面颊毛孔和细纹仍清晰，没有蜡质涂抹`。若选择
`not_applicable`，也要写对应对象为何不可见，例如：
`人物闭眼且没有露齿，眼白和牙齿均不可见，无法判断蓝偏色`。不要为了通过校验
堆砌词语；备注必须与当前 proof 中真实可见的内容一致。

运行审查：

```bash
python3 retouch-travel-portrait/scripts/audit_result.py \
  portrait-run/original.jpg portrait-run/checkpoints/<current-file> \
  --mode natural \
  --manual-review portrait-run/manual-review.json \
  --proof-manifest portrait-run/proof/manifest.json \
  --output portrait-run/audit.json

python3 retouch-travel-portrait/scripts/pipeline_state.py finalize \
  --run-dir portrait-run \
  --audit portrait-run/audit.json
```

proof 文件、候选或审查 JSON 一旦被替换或修改，哈希绑定会失效，不能完成最终输出。

100% 细节 proof 同时提供两组原图/候选像素级局部：第一组使用保守的上半身肤色密度启发式，第二组固定覆盖上方中央区域。两组都必须查看；第二组用于降低主焦点误落在手臂、服装或同色背景上的风险。它们只是审查辅助，不构成人脸检测或身份认证。

## 文件夹工作流

准备队列：

```bash
python3 retouch-travel-portrait/scripts/batch_pipeline_v6.py prepare \
  input-portraits output-queue \
  --mode natural
```

每次完成当前人工/Agent 视觉决定后，继续队列：

```bash
python3 retouch-travel-portrait/scripts/batch_pipeline_v6.py resume \
  input-portraits output-queue
```

一次 `resume` 对每个独立 run 最多生成一个待审的确定性候选，或在进入最终阶段时生成一套 proof。它不会自动 accept、不会替人填写审查备注、不会调用图片模型、不会自动 finalize。

只刷新状态，不推进工作：

```bash
python3 retouch-travel-portrait/scripts/batch_pipeline_v6.py status \
  input-portraits output-queue
```

队列会分别报告 `complete`、`safe_but_subtle`、`partial_v3_fallback`、`failed`、`awaiting_review` 与 `prepared`。旧的 `batch_retouch.py` 只是 V3 局部保守回退，不是完整 V6 批量器。

## 可选模型路径的边界

模型路径采用以下状态机：

```text
reserve -> complete -> accept
                    -> reject
        -> fail
```

必须在外部调用前 `reserve-model-attempt`。服务成功返回后使用 `complete-model-attempt` 绑定候选；服务失败使用 `fail-model-attempt`；视觉不合格使用 `reject-model-attempt`；只有 proof 与十项审查已接受时才能使用 `accept-model-candidate`。

失败、拒绝或成功都会永久消耗这一次记录名额，不能在同一个 run 中再次 reserve。这个状态机能够约束通过 Skill 记录的工作流，但无法从外部服务层独立证明操作者没有绕过 Skill 私下再次调用模型。

## 隐私与安全

- 在支持 POSIX 权限的平台上，run/queue 目录使用 `0700`，其中的文件使用 `0600`。
- run 目录是敏感数据：它包含不可变原图副本，并可能保留相机元数据（包括 EXIF/GPS）、中间 checkpoint、proof、备注和模型产物。
- 默认确定性路径不需要把图片上传到模型服务；可选模型路径可能把原图交给第三方，应先取得许可并确认其保存政策。
- 不要把真实照片、run、proof、队列 manifest 或元数据提交到 GitHub 或放入发布包。

## Beta 限制

- 这不是无人值守的自动美颜服务；每个视觉门都需要人或具备视觉能力的 Agent。
- 当前不提供换脸、塑形、自动身份认证、背景替换或几何纠正。
- 自动指标只能拒绝明显风险，不能替代审美与身份一致性判断。
- 本版本不宣称真实世界 90% 成功率，也不把少量样本结果外推为生产可用性。
- `natural`、`fresh` 与 `warm-film` 都受相同安全边界约束；任何模式都不能保证适合每一种肤色、光照、遮挡、妆容或相机条件。

## v0.7.0-beta 聚焦真实图验证

本次发布前用 12 张公开单人女性人像执行了完整 `natural` V6 前向验证，覆盖柔光、窗边、低照度、强红光、黑白、户外、传统服装、霓虹散景与高反差场景。结果为：

- 输入预检 12/12 通过，最终工作流 12/12 完成，十项审查 12/12 接受；
- 12/12 最终画布尺寸与原图显示画布精确一致；
- 图片模型调用 0 次；
- 36 个确定性阶段候选中，33 个经视觉审查接受，2 个因像素完全无变化转为安全 `no_change`，1 个皮肤候选因肤色蒙版触及粉色花朵而被拒绝并回滚；
- 该组候选接受率为 33/36（91.7%），工作流完成率为 12/12；这两个数字只描述该次小样本、人工/视觉 Agent 审查实验，不能外推为真实世界 90% 成功率或无人值守能力。

测试图片、原图副本、proof 和 EXIF/GPS 元数据不会进入 GitHub 仓库或发布包。完整发布门与样本限制见 `VALIDATION.md`，逐阶段聚合结果见 `REAL_IMAGE_VALIDATION.md`，脱敏机器可读结果见 `validation-summary.json`。

发布验证方法见 `VALIDATION.md`，完整 Agent 操作协议见 `SKILL.md`。许可证见仓库根目录 `LICENSE`。
