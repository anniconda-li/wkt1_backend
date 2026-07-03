# knowledge

运行时只读取本地 JSON，不再调用远程视觉知识库应用。

```text
knowledge/config/exhibit_cards.json             手工维护：问答用知识卡、历史背景、常见追问、禁讲规则
knowledge/config/museum_vision_candidates.json  手工维护：视觉匹配用文物 ID、名称、匹配关键词、基准图路径
knowledge/config/vision_profiles.json           自动生成：视觉模型读取 photo/ 后产出的详细视觉档案
```

`exhibit_cards.json` 是 AI 问答的主要本地资料源。运行时如果它不存在，才会从
`museum_vision_candidates.json` 退化生成轻量卡片。

更新 `photo/` 基准照片或候选配置后，重新生成视觉档案：

```powershell
python tools\build_vision_profiles.py --overwrite
```

`vision_profiles.json` 是生成物，但会随项目提交，设备运行时直接读取它做本地匹配。
