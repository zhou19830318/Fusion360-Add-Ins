# AI Fusion — 插件架构与设计流程文档

---

## 1. 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     FUSION 360 进程                               │
│                                                                    │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────────────┐ │
│  │ AdamFusion   │───▶│ bridge/      │───▶│ handlers/              │ │
│  │ .py          │    │ palette.pyc   │    │ __init__.pyc          │ │
│  │ (入口)       │    │ (消息桥接)   │    │ (工具路由: dispatch)  │ │
│  │              │    │              │    │                       │ │
│  │ 启动线程 ────┼───▶│ 接收    │    │    │ read.pyc     ──查询  │ │
│  │ Flask 服务器  │    │ HTML→Python  │    │ create.pyc   ──创建  │ │
│  │ 端口 8765    │    │ ←─Python→HTML │    │ update.pyc   ──修改  │ │
│  │              │    │              │    │ delete.pyc   ──删除  │ │
│  └──────┬───────┘    └──────────────┘    │ execute.pyc  ──执行  │ │
│         │                                └───────────────────────┘ │
│         │ (守护线程)                                                 │
│  ┌──────▼───────────────────────────────────────────────────────┐ │
│  │  local_server/server.py (Flask)     端口:8765                  │ │
│  │                                                               │ │
│  │  GET  /chat          → 返回 chat_ui.html (聊天界面HTML)      │ │
│  │  POST /api/chat      → 转发到 DeepSeek/Kimi AI 并返回结果    │ │
│  │  GET  /api/models    → 返回可用模型列表                      │ │
│  │  GET  /api/config    → 读写配置 (API key, 模型, system_prompt)│ │
│  │                                                               │ │
│  │  config.json ← 运行时加载                                    │ │
│  │    ├─ provider: deepseek / kimi                              │ │
│  │    ├─ model: deepseek-v4-flash / ...                         │ │
│  │    ├─ deepseek_api_key: sk-...                               │ │
│  │    └─ system_prompt: (AI 系统提示词)                         │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  Fusion Palette (Qt WebEngine)                                  │ │
│  │  ┌──────────────────────────────────────────────────────────┐  │ │
│  │  │  chat_ui.html                                             │  │ │
│  │  │  ┌─────────────────────────────────────────────────────┐ │  │ │
│  │  │  │ 聊天框 UI (Markdown 渲染)                            │ │  │ │
│  │  │  │  └─ 用户发消息 → POST /api/chat → DeepSeek          │ │  │ │
│  │  │  │  └─ AI 返回 tool_calls → adsk.fusionSendData()      │ │  │ │
│  │  │  │       └─ payload = {name: "read", args: {...}}      │ │  │ │
│  │  │  │         → bridge → handlers → Fusion API            │ │  │ │
│  │  │  └─────────────────────────────────────────────────────┘ │  │
│  │  │  ⚙ 设置面板: 切换 provider/model/API key/sys prompt     │  │
│  │  └──────────────────────────────────────────────────────────┘  │
│  └────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. 文件清单与职责

| 文件 | 类型 | 职责 |
|------|------|------|
| `AdamFusion.py` | Python | 插件入口: 启动 Flask, 创建 Palette, 注册工具栏按钮 |
| `AdamFusion.manifest` | JSON | Fusion 加载清单 (id, 名称, 版本, runOnStartup等) |
| `local_server/server.py` | Python | Flask 后端: 提供聊天 UI, 代理 AI API, 读写配置 |
| `local_server/chat_ui.html` | HTML+JS | 聊天界面: 对话交互, 工具调用桥接, 设置面板 |
| `local_server/config.json` | JSON | 运行配置: AI provider, API key, model, system_prompt |
| `resources/palette.html` | HTML+JS | 加载页面: 轮询等待服务器就绪后跳转到 chat |
| `bridge/palette.pyc` | Python | 消息桥: 接收 adsk.fusionSendData 事件, 解析载荷, 分派工具 |
| `bridge/selection_watcher.pyc` | Python | 选择监听: 监控 Fusion 中用户选中实体的变化 |
| `handlers/__init__.pyc` | Python | 工具路由器: `dispatch(tool_name, args)` 映射到对应 handler |
| `handlers/execute.pyc` | Python | 执行 Python 脚本, 打开/关闭/保存文档 |
| `handlers/read.pyc` | Python | 查询模型: bodies, features, timeline, 尺寸, API docs 等 |
| `handlers/create.pyc` | Python | 创建简单特征: box, cylinder, extrude, fillet 等 |
| `handlers/update.pyc` | Python | 修改特征: 参数, 可见性, undo/redo, 时间轴位置 |
| `handlers/delete.pyc` | Python | 删除特征或实体 |
| `handlers/_common.pyc` | Python | 公共工具: token 压缩, 向量/平面辅助函数 |
| `handlers/api_documentation.pyc` | Python | API 文档查询: 搜索 Fusion API 函数/类签名 |
| `install_deps.bat` | Batch | 依赖安装助手: 自动找到 Fusion Python 并 pip install |

---

## 3. 启动流程

```
用户: Fusion 360 菜单 → 工具 → 附加模块 → Run AI Fusion
  │
  ▼
AdamFusion.run(_context)
  │
  ├─ 1. _ensure_dependencies()
  │      ├─ import flask, requests
  │      ├─ 如果缺失 → _find_real_python() → pip install
  │      └─ pip 缺失 → ensurepip.bootstrap() → pip install
  │
  ├─ 2. _start_server()
  │      ├─ addin_root → sys.path.insert(0, ...)
  │      ├─ from local_server.server import create_app
  │      ├─ threading.Thread(Flask, daemon=True).start()
  │      └─ 轮询 http://127.0.0.1:8765/api/models (最多 20s)
  │
  ├─ 3. 创建 Palette
  │      ├─ ui.palettes.add(PALETTE_ID, ...)
  │      ├─ palette.open("http://127.0.0.1:8765/chat")
  │      └─ palette_bridge.attach(pal, _handlers)
  │
  └─ 4. 注册工具栏按钮 (Design → Utilities → Add-Ins)
```

**关键设计决策**: Flask 运行在 Fusion 进程内的**守护线程**中（非子进程），避免了 Python 路径问题。

---

## 4. 辅助设计工作流（AI 与用户协作）

```
┌──────────────────────────────────────────────────────────────┐
│                  用户发送消息                                  │
│  "帮这个 PCB 设计一个外壳, 壁厚1.6mm, 留USB和SD卡开口"        │
└─────────────────────┬────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────┐
│  step 1: 对话管理 (chat_ui.html)                             │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ conversation = [{role:"system", content:sys_prompt},     ││
│  │                 {role:"user",   content:"帮这个PCB..."}] ││
│  │ ↓ POST /api/chat {model, messages, tools}               ││
│  └──────────────────────────────────────────────────────────┘│
└─────────────────────┬────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────┐
│  step 2: Flask 代理 (server.py)                               │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ ① 从 config.json 读取 system_prompt, 插入消息首位         ││
│  │ ② 从 config.json 读取 API key, model, provider            ││
│  │ ③ POST https://api.deepseek.com/chat/completions         ││
│  │     Headers: {"Authorization":"Bearer sk-..."}            ││
│  │     Body: {model, messages, tools}                       ││
│  │     tools = FUSION_TOOLS (read/execute/create/update/del)││
│  │ ④ 返回 AI 响应 → chat_ui.html                            ││
│  └──────────────────────────────────────────────────────────┘│
└─────────────────────┬────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────┐
│  step 3: AI 决策 (DeepSeek)                                   │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ AI 从 system_prompt 获得:                                 ││
│  │  - 工作流: Probe → Measure → Plan → Build → Verify       ││
│  │  - API 模板: do(), rrect(), newsk()                     ││
│  │  - API 陷阱: ThroughAllExtent❌, occurrence.name❌        ││
│  │                                                          ││
│  │ AI 返回: {role:"assistant", tool_calls:[                  ││
│  │   {function:{name:"read", args:{queryType:"bodies"}}}     ││
│  │ ]}                                                        ││
│  └──────────────────────────────────────────────────────────┘│
└─────────────────────┬────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────┐
│  step 4: 工具执行 (chat_ui.html → bridge → handlers)         │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ JS: adsk.fusionSendData("tool.call",                      ││
│  │       JSON.stringify({name:"read", args:{queryType:"..."}}││
│  │       ))                                                  ││
│  │   ↓                                                      ││
│  │ bridge/palette.pyc: _IncomingHandler.notify()             ││
│  │   → parse action="tool.call"                             ││
│  │   → payload.get("name") → "read"                         ││
│  │   → payload.get("args") → {queryType:"bodies"}           ││
│  │   → handlers/__init__.pyc: dispatch("read", args)         ││
│  │   → handlers/read.pyc: handle(args)                       ││
│  │   → adsk.fusion API: root.bRepBodies.count, etc.         ││
│  │   → 返回 JSON 结果 (ok, payload)                          ││
│  │   ↓                                                      ││
│  │ args.returnData = JSON.stringify({ok:true, payload:...})  ││
│  │ → JS: resolve(JSON.parse(data))                          ││
│  │ → conversation.push({role:"tool", content:result})        ││
│  │ → 回到 step 1 继续循环 (最多 10 轮)                      ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

---

## 5. PCB 外壳设计专用流程

```
用户: "帮这个PCB设计外壳"
  │
  ▼
1️⃣ Probe (探测)  ── AI 调用 read(bodies)
  │  找出 PCB 实体 → 获取 bounding box → 确定长x宽x厚
  │
  ▼
2️⃣ Measure (测量) ── AI 调用 execute(script) 
  │  遍历 root.allOccurrences
  │  测每个元件的 bounding box, 记录最高 Z, 最低 Z
  │  识别关键元件 (J1=USB, J2=SD卡, J3=FPC, Key1/2=按钮, MIC, LED)
  │  输出元件位置表给用户
  │
  ▼
3️⃣ Plan (规划) ── AI 呈现给用户:
  │  PCB: 65x30x1.6mm, 最高元件: 8.5mm(排针), 底面最深: -3.5mm(SD卡)
  │  建议: 壁厚 1.6mm, 间隙 0.2mm, 外壳 69x34mm
  │  询问: 需要哪些开口? (USB, SD, FPC, 排针, 按钮, MIC, LED...)
  │  ⬆ 用户确认/调整
  │
  ▼
4️⃣ Build (构建) ── AI 执行 execute(script):
  │  下壳: 外形(69x34mm) + 挖内腔 + PCB支撑台 + M2螺柱 + SD开口
  │  上壳: 外形 + 低高区台阶 + 定位舌 + 螺柱 + 螺丝孔 + 全部侧开孔
  │  一次性在一个 execute 调用中完成
  │
  ▼
5️⃣ Verify (验证) ── AI 调用 execute(script):
  │  检查 Case vs 每个元件的干涉
  │  测量关键间隙 (>0.2mm 才算安全)
  │  输出结果汇总
  │
  ▼
6️⃣ Refine (精调) ── 如需要:
  │  调整开口位置/尺寸
  │  添加散热槽
  │  应用材料外观
  │  适配摄像机视角
```

---

## 6. 工具调用数据格式

### JS → bridge (adsk.fusionSendData)
```json
{
  "name": "execute",
  "args": {
    "featureType": "script",
    "object": {
      "script": "def run(_context):\n    ..."
    }
  }
}
```

### bridge → handler
```python
# bridge/palette.pyc 从 payload 中取:
tool_name = payload.get("name")        # "execute"
tool_args = payload.get("args") or {}  # {featureType:"script", object:{...}}

# dispatch 路由:
handler_dispatch(tool_name, tool_args)
# → handlers/execute.pyc: handle(tool_args)
```

### ⚠️ 关键: execute 的两层参数结构
```python
def handle(args):
    ft = args.get("featureType")   # "script"
    obj = args.get("object") or {} # {script: "def run..."}
    if ft == "script":
        return _script(obj)        # obj.get("script")
```

---

## 7. 系统提示词 (config.json system_prompt)

AI 每次对话都会收到的核心指令:

| 模块 | 内容 |
|------|------|
| 身份 | AI copilot for Autodesk Fusion 360 |
| 工作流 | Probe → Measure → Plan → Build → Verify → Refine |
| 模板代码 | `do()` 挤出通用函数, `rrect()` 圆角矩形, `newsk()` 延迟草图 |
| API 陷阱 | ThroughAllExtent不可用/ occurrence.name只读 / CUT不传participantBodies |
| 单位 | Fusion cm, 用户 mm, 常量 `MM=0.1` |
| 验证要求 | 打印 bounding box, 干涉检查, 间隙测量 |

---

## 8. 核心 API 模式 (已反复验证)

```python
MM = 0.1
def P(x, y, z=0.0): return adsk.core.Point3D.create(x*MM, y*MM, z*MM)
def vi(v):           return adsk.core.ValueInput.createByReal(v*MM)

NEW = adsk.fusion.FeatureOperations.NewBodyFeatureOperation
JOIN = adsk.fusion.FeatureOperations.JoinFeatureOperation
CUT  = adsk.fusion.FeatureOperations.CutFeatureOperation
POS  = adsk.fusion.ExtentDirections.PositiveExtentDirection

def newsk(comp):
    s = comp.sketches.add(comp.xYConstructionPlane)
    s.isComputeDeferred = True
    return s

def do(comp, sk, z0, z1, op, bodies=None):
    sk.isComputeDeferred = False
    col = adsk.core.ObjectCollection.create()
    for p in sk.profiles: col.add(p)
    ei = comp.features.extrudeFeatures.createInput(col, op)
    ei.startExtent = OffsetStartDefinition.create(vi(z0))
    ei.setOneSideExtent(DistanceExtentDefinition.create(vi(z1-z0)), POS)
    # CUT 操作不传 participantBodies → let Fusion auto-detect
    if bodies is not None and op != CUT:
        ei.participantBodies = bodies
    return comp.features.extrudeFeatures.add(ei)
```

---

## 9. 关键故障模式与解决方案

| # | 故障 | 根因 | 解决方案 |
|---|------|------|----------|
| 1 | `ERR_CONNECTION_REFUSED` | Flask 未启动 (pip 没有 flask) | `ensurepip.bootstrap()` + `pip install flask requests` |
| 2 | `ModuleNotFoundError: local_server` | sys.path 不含插件目录 | `sys.path.insert(0, addin_root)` |
| 3 | `ai provider 400` | deepseek-chat 已废弃 | 改用 `deepseek-v4-flash` |
| 4 | `unknown tool: None` | JS 发了 `{tool_name:...}` 但 bridge 取 `{name:...}` | 改为 `{name: toolName}` |
| 5 | `execute script requires non-empty object.script` | JS 发了 `{script:...}` 但 handle 取 `object.script` | 改为 `{object: {script: ...}}` |
| 6 | CUT 操作 health=1 失败 | participantBodies 指向旧 body 引用 | CUT 时不传 participantBodies |
| 7 | `ThroughAllExtent.create(distance)` 崩溃 | 该方法不接受参数 | 使用 DistanceExtentDefinition 代替 |
| 8 | `occurrence.name = ...` 崩溃 | name 属性只读 | 使用 `occurrence.component.name = ...` |
| 9 | config.json JSON 解析失败 | system_prompt 内含未转义引号 | 用 json.dump() 生成 |

---

## 10. 配置说明

### config.json
```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "deepseek_api_key": "sk-...",
  "deepseek_base_url": "https://api.deepseek.com",
  "kimi_api_key": "",
  "kimi_base_url": "https://api.moonshot.cn/v1",
  "server_port": 8765,
  "system_prompt": "You are AI Fusion..."
}
```

### 环境变量覆盖
- `AIFUSION_CHAT_URL=http://remote:3001/chat` → 使用远程聊天 UI (跳过本地服务器)
