# Retouch Travel Portrait V6：12 张 natural 模式独立验证报告

验证日期：2026-08-09
被验证版本：`v0.7.0-beta`
样本规模：12 张
模式：`natural`

## 结论

本轮 12 个独立 V6 run 均通过状态机复核并以 `complete` 结束；12 份最终审计均为 `accept`，12 份当前 proof manifest 均通过文件哈希与输入绑定复核。队列没有失败、待审、V3 回退或模型调用。

这组结果支持的结论是：当前版本已经能在**人工或视觉 Agent 逐候选审核**的前提下，稳定跑通一组自然、克制、可回滚的本地批处理工作流。它不支持以下说法：无人值守自动出片、一般化 90% 成功率、每张图都有肉眼明显提升、已经生产就绪。

关键数字：

- 工作流完成：12/12（仅代表本组样本的 workflow completion）。
- 本地确定性候选：36 个；视觉接受 33 个，拒绝并回滚 1 个，零像素变化 2 个。
- 候选接受率：33/36，即 91.7%。这是**候选级**比例，不是图片级成功率，也不是可感知提升率。
- 审计：12/12 `accept`；所有接受都依赖 proof-bound 人工审查，0 个 run 可自动接受。
- 模型调用：0；本轮只测试本地确定性路径。
- 几何与画布：36/36 候选与其输入画布尺寸完全相同，宽高比偏差均为 0；12/12 最终图也保持原画布尺寸。
- Proof：12/12 当前 manifest 为 schema 2，均通过状态与哈希复核。

## 四类指标必须分开理解

| 指标 | 本轮结果 | 能说明什么 | 不能说明什么 |
|---|---:|---|---|
| Workflow completion | 12/12 | 固定阶段、人工闸门、proof、审计和最终化都能完成 | 不等于每个候选都好，也不等于无人值守 |
| Candidate acceptance | 33/36（91.7%） | 本组样本中多数本地候选经视觉审查可保留 | 不等于一般化成功率或图片通过率 |
| Final audit acceptance | 12/12 | 回滚/零变化后，最终保留结果满足当前十项审查 | 不等于自动审美判定；自动层明确禁止 auto-accept |
| Perceptible improvement | 未单独评分 | 可确认改动克制、非零、需 proof 辅助比较 | 没有盲测，不能给出“明显变好”的百分比 |

## 阶段结果

下表同时区分“run 的最终阶段状态”和“该阶段生成过的候选结果”。例如 case-08 的皮肤候选被拒绝后，阶段最终状态是 `no_change`，因为工作流保留了上一个安全 checkpoint。

| 阶段 | 最终 accepted | 最终 no_change | 生成候选 | 候选接受 | 候选拒绝 | 零像素候选 | 候选接受率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| photo_correction | 0 | 12 | 0 | 0 | 0 | 0 | 不适用 |
| light_balance | 12 | 0 | 12 | 12 | 0 | 0 | 100% |
| color_balance | 11 | 1 | 12 | 11 | 0 | 1 | 91.7% |
| skin_cleanup | 10 | 2 | 12 | 10 | 1 | 1 | 83.3% |
| facial_features | 0 | 12 | 0 | 0 | 0 | 0 | 不适用 |
| hair_clothing | 0 | 12 | 0 | 0 | 0 | 0 | 不适用 |
| background | 0 | 12 | 0 | 0 | 0 | 0 | 不适用 |
| final_review | 12 | 0 | 0 | 0 | 0 | 0 | 不适用 |

`photo_correction`、`facial_features`、`hair_clothing` 和 `background` 在这组默认 natural 参数下属于有记录的检查型 no-op；它们并非被跳过，而是逐图写入了不修改的图像特定理由。

## 12 个 run 的最终状态

缩写：P=photo correction，L=light，C=color，S=skin，F=facial，H=hair/clothing，B=background，R=final review。

| Run | 画布 | P | L | C | S | F | H | B | R | 最终审计 |
|---|---:|---|---|---|---|---|---|---|---|---|
| case-01 | 1600×2400 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-02 | 1600×1067 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-03 | 1600×2400 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-04 | 1600×2217 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-05 | 1600×2400 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-06 | 1600×1068 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-07 | 1600×2949 | no_change | accepted | no_change | no_change | no_change | no_change | no_change | accepted | accept |
| case-08 | 1600×2131 | no_change | accepted | accepted | no_change¹ | no_change | no_change | no_change | accepted | accept |
| case-09 | 1600×1067 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-10 | 1600×2400 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-11 | 1600×2400 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |
| case-12 | 1600×2400 | no_change | accepted | accepted | accepted | no_change | no_change | no_change | accepted | accept |

¹ case-08 的皮肤候选先被明确拒绝，随后回滚并记录安全 `no_change`。

## 自动检查与人工审计

12 份审计的统一结果：

- 自动预检：12/12 `pass_with_review`。
- 自动 hard reject：0；自动 review flag：0。
- 十项检查中的自动状态：36 项 `pass`，84 项 `not_evaluable`。
- 十项检查中的人工状态：111 项 `pass`，9 项有图像特定说明的 `not_applicable`，0 项 `fail`。
- 最终解析状态：111 项 `pass`，9 项 `not_applicable`，0 项 `fail`。
- 人工审查完整：12/12。
- Proof 验证：12/12。
- `may_auto_accept`：0/12；该值在所有 run 中都是 `false`。

84 个 `not_evaluable` 不是漏审。它表示自动指标无法可靠判断皮肤纹理、脸颈肤色、眼白/牙齿、唇色、发丝边缘等语义问题，因此这些项目由绑定当前 proof 的人工审查解决。最终 `accept` 不能由自动指标单独产生。

## 画布、几何和拉伸风险

- 36 个候选全部通过几何 gate。
- 36 个候选的编码宽高与各自输入 checkpoint 完全一致。
- 候选宽高比相对偏差最小值和最大值均为 0。
- 12 张最终图与各自原图的编码宽高完全一致，最终宽高比偏差最大值为 0。
- 本轮未观察到拉伸、额外画布、横竖方向错误或分辨率降级。

这些数据可以证明编码画布没有变形，但不能单独证明人物五官或身体比例没有语义漂移；后者仍由 side-by-side、100% 细节、缩略图和手机预览人工检查。

## 两个重点异常路径

### case-08：皮肤候选拒绝与回滚

皮肤候选的 32× difference 观察显示，颜色蒙版从脸和手扩展到了粉色樱花及其他非皮肤区域。虽然数值改动很小，但它违反了“局部皮肤清理”的范围要求，因此被拒绝。

回滚已由状态哈希核验：

- 被拒绝候选 SHA 与最终图 SHA 不同。
- 皮肤阶段保留 checkpoint 的 SHA 等于 color balance checkpoint 的 SHA。
- 最终图 SHA 等于该 color balance checkpoint 的 SHA。
- 因而被拒绝的皮肤候选没有进入最终结果。

这验证了批处理中的失败隔离与保守回滚行为，而不是把局部失败伪装成候选通过。

### case-07：黑白图的零变化

case-07 的 color balance 候选和 skin cleanup 候选都是 `no_pixel_change`：颜色阶段不向灰度图注入色彩，皮肤颜色蒙版也没有产生修改。两阶段均正确记录 `no_change`。

需要准确说明的是：case-07 的整个 run 并非零变化。其 restrained light balance 候选经视觉审查接受，最终图相对原图的 RGB MAE 为 0.00468444；“零变化”只适用于 color 和 skin 两个阶段。

## Proof 焦点修复后的状态

当前 12 份 proof 均由 `portrait-retouch-render-proof-v6` 生成，manifest schema 为 2。每个 run 包含：

- 5 个独立 proof 文件，绑定 6 种审查视图；
- 512×512 的 100% 细节裁切；
- 两个焦点：`skin_density_primary` 和 `upper_center_fallback`；
- 原图、最终候选、manifest 和每个 proof 文件的哈希绑定。

独立查看 v2 细节联系表后，12/12 case 的双焦点组合都提供了可用于检查脸、眼睛或皮肤纹理的区域。修复是有效的，但仍有一个明确边界：primary skin-density heuristic 在 case-04、05、07、10、11 没有单独落到理想面部区域，实际由 upper-center fallback 提供可审查的人脸细节。

因此当前状态应描述为“**双焦点降级策略可用，primary 启发式仍不应脱离 fallback 和人工观察单独使用**”，而不是“焦点检测已经完全解决”。所有 12 份最终审计都绑定在修复后的当前 manifest 上，并通过重新验证。

## 自然度与可感知提升

本轮最终图相对原图均有非零测量变化，但变化很小：

- RGB MAE：0.00193957–0.010891，平均 0.0073878492。
- Luma MAE：0.00195392–0.00888173，平均 0.0060530158。
- Luma correlation：0.99986434–0.99996717。

结合 side-by-side、difference、100% detail、thumbnail 和 phone proof，本轮证据支持“克制、自然、未出现明显过度修改”。不少变化只有并排对比或差异图下才容易辨认，这是 natural 默认模式的设计结果。

本轮没有独立的盲测偏好标签，也没有要求评审者对“原图/结果哪张更好”进行强制二选一。因此不能从现有证据计算“可感知提升通过率”，更不能把 91.7% 候选接受率改写为“91.7% 的图片明显变好”。若要得到该指标，需要另做隐藏版本顺序、多人评分的 A/B 测试。

## 接入工作流后，本版本能做到的程度

本轮已验证：

- 对文件夹建立稳定的逐图独立队列；
- 固定顺序推进 8 个阶段；
- 默认使用 0 次图片模型调用的本地确定性路径；
- 每次仅生成待审候选，不自动接受；
- 对无意义候选记录零变化，对不安全候选拒绝并回滚；
- 保持画布与宽高比；
- 生成可哈希复核的多视图 proof；
- 完成十项人工审计后才最终化；
- 单张候选失败不会迫使整批错误接受。

尚未由本轮证明：

- 无人值守批量自动接受；
- 对任意来源、任意光线、任意构图的一般化通过率；
- 外部图片模型路径的质量、稳定性、隐私和成本表现；
- 自动成年人识别、身份一致性认证或法律合规认证；
- 多人主脸、儿童、透明/动画、几何重塑等明确排除范围；
- 生产级吞吐、故障恢复、长期监控和多人审片一致性。

## 发布边界

该结果适合支持 `v0.7.0-beta` 的公开 beta 定位，前提是 README 和 release notes 明确要求逐候选视觉审核，并保留上述限制。

GitHub 仓库和发布附件中不应包含这 12 张真实肖像、`inputs/`、`queue/runs/`、proof 图片、审核笔记、EXIF/GPS 或队列中的绝对本地路径。公开材料只应使用不含肖像和敏感运行元数据的聚合报告、合成 fixture、测试代码及机器可读的脱敏汇总。

机器可读汇总见 `validation-summary.json`。
