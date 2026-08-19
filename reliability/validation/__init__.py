"""validation — 四层验证（需求 §10：Schema / 设计意图 / 几何 / 视觉）

MVP 实现 Schema(spec/schema.py) + 设计意图 + 基础几何；视觉验证在 executor 截图后
交由带视觉能力的模型做证据级检查（需求 §1.7: 视觉仅作验证证据）。
"""