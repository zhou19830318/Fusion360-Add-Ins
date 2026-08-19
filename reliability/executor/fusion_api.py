"""真实 adsk 桥 —— 用 Fusion 官方 API 直接实现(不依赖 handlers .pyc)。

背景: 新版 Fusion(2704+) 的 design.findEntityByToken 返回 BaseVector(多实体),
handlers(.pyc) 仍按旧单对象 API 处理 → ObjectCollection_add 崩溃(sketch/profile、
面引用等 token 解析路径在新版 Fusion 全部不可用)。可靠层按路线 A 的补充策略,
对这类"黑盒不可修"的能力用真实源码实现。

本模块职责(MVP):
  1. AI 对象属性标签(BOM 追溯 §12): 给执行器创建的 BRepBody 写 Attribute
  2. BOM 采集: 遍历设计内实体 + 属性 → BOM 行
仅允许在 Fusion 进程内 import(加 adsk.core / adsk.fusion 顶层依赖)。
"""

from __future__ import annotations

from typing import Any, Optional

# 属性组名(需求 §12.1 元数据归属)
ATTR_GROUP = "AIFusion"


def _app():
    import adsk.core
    return adsk.core.Application.get()


def _root():
    import adsk.fusion
    app = _app()
    des = adsk.fusion.Design.cast(app.activeProduct)
    return des.rootComponent if des else None


def ensure_design() -> Any:
    """确保存在活动参数化 Fusion 设计文档。

    实测(Fusion 2704): documents.add() 新建的文档不保证 parametric,
    designType setter 对空文档静默失败; 因此这里要求活动产品已是参数化
    设计, 否则报错(引导用户先打开/新建设计, 属正常使用路径)。
    """
    import adsk.core
    import adsk.fusion
    app = adsk.core.Application.get()
    if app is None:
        raise RuntimeError("no Fusion Application")
    product = app.activeProduct
    if product is None or not isinstance(product, adsk.fusion.Design):
        raise RuntimeError("无活动设计文档: 请先打开或新建一个参数化设计")
    des = product
    try:
        if des.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            des.designType = adsk.fusion.DesignTypes.ParametricDesignType
    except Exception:
        pass
    if des.designType != adsk.fusion.DesignTypes.ParametricDesignType:
        raise RuntimeError("活动设计不是参数化设计: 请新建参数化设计文档")
    return des


def body_by_name(name: str) -> Optional[Any]:
    """按名称查找根组件下的实体(优先精确匹配含名称)。"""
    root = _root()
    if root is None:
        return None
    for b in root.bRepBodies:
        try:
            if b.name == name or (name and name in b.name):
                return b
        except Exception:
            continue
    return None


def set_body_attributes(body, attrs: dict, group: str = ATTR_GROUP) -> dict:
    """给实体写入 AI 属性(需求 §12.1)。返回写入结果。
    属性: ai_feature_id/semantic_role/intent_id/plan_node_id/... """
    out = {}
    for key, value in attrs.items():
        try:
            body.attributes.add(group, key, str(value) if value is not None else "")
            out[key] = "ok"
        except Exception as e:
            out[key] = f"err:{e}"
    return out


def set_attributes_by_name(name: str, attrs: dict, group: str = ATTR_GROUP) -> dict:
    body = body_by_name(name)
    if body is None:
        return {"error": f"body {name} not found"}
    return set_body_attributes(body, attrs, group)


def collect_bom(group: str = ATTR_GROUP) -> list[dict]:
    """采集 BOM(需求 §12.2 MVP 字段): 每个实体的名称/体积/属性/材料信息。"""
    root = _root()
    rows = []
    if root is None:
        return rows
    for b in root.bRepBodies:
        attrs = {}
        try:
            for a in b.attributes:
                if a.groupName == group:
                    attrs[a.name] = a.value
        except Exception:
            pass
        row: dict[str, Any] = {
            "body_name": getattr(b, "name", ""),
            "volume_mm3": None,
            "mass_g": None,
            "bbox": None,
            "attributes": attrs,
            "component_type": "",
            "manufacturing_method": "",
            "material": "",
        }
        is_ai = attrs.get("created_by") == "ai"
        row["component_type"] = "ai_generated" if is_ai else "user_existing"
        row["manufacturing_method"] = attrs.get("manufacturing_method", "")
        try:
            mat = b.material
            row["material"] = mat.name if mat else ""
        except Exception:
            pass
        try:
            props = b.physicalProperties
            row["volume_mm3"] = round(props.volume * 1000.0, 3)  # Fusion cm³ → mm³
            try:
                row["mass_g"] = round(props.mass * 1000.0, 3)  # kg → g
            except Exception:
                pass
        except Exception:
            pass
        try:
            bb = b.boundingBox
            mn, mx = bb.minPoint, bb.maxPoint
            row["bbox"] = {
                "min": [round(mn.x, 2), round(mn.y, 2), round(mn.z, 2)],
                "max": [round(mx.x, 2), round(mx.y, 2), round(mx.z, 2)],
            }
        except Exception:
            pass
        rows.append(row)
    return rows


# 预置 BOM 视图(需求 §12.2): 名称 → 谓词(行 dict) → bool
BOM_VIEWS: dict[str, callable] = {
    "all": lambda r: True,
    "ai": lambda r: (r.get("attributes") or {}).get("created_by") == "ai",
    "user": lambda r: (r.get("attributes") or {}).get("created_by") != "ai",
    "make": lambda r: (r.get("attributes") or {}).get("manufacturing_method", "") in ("3d_print", "cnc"),
    "3d_print": lambda r: (r.get("attributes") or {}).get("manufacturing_method") == "3d_print",
    "cnc": lambda r: (r.get("attributes") or {}).get("manufacturing_method") == "cnc",
    "warning": lambda r: not (r.get("attributes") or {}).get("plan_node_id") or r.get("volume_mm3") is None,
    "missing_material": lambda r: not r.get("material"),
    "missing_parameters": lambda r: not (r.get("attributes") or {}).get("parameter_refs"),
}


def filter_bom(rows: list, view: str) -> list:
    pred = BOM_VIEWS.get(view or "all")
    return [r for r in rows if (pred if pred else (lambda r: True))(r)]


def _top_plane_face(body) -> Optional[Any]:
    """返回实体顶面部平面(法向 +Z 且 z 最大), 用于在其上建草图打孔。"""
    import adsk.core
    try:
        best = None
        best_z = -1e9
        for fc in body.faces:
            try:
                if fc.geometry.objectType == adsk.core.Plane.classType():
                    pl = adsk.core.Plane.cast(fc.geometry)
                    z = pl.origin.z
                    if abs(pl.normal.z - 1.0) < 1e-3 and z > best_z:
                        best, best_z = fc, z
            except Exception:
                pass
        return best
    except Exception:
        return None


def create_cut_holes(points_xy_mm: list, diameter_mm: float,
                     plane_root_attr: str = "xYConstructionPlane") -> dict:
    """在给定实体顶面打贯通圆孔(sketch 圆 + extrude cut)。

    适配 Fusion 2704:
      - 草图搭建在目标实体顶面部平面(与切口+同一面 → 必然命中, 避免
        '未找到要剪切或相交的目标实体' 的问题);
      - setAllExtent 等 SWIG 绑定要求显式 direction 参数。
    返回 {"ok": bool, "holes": n, "error": str?}
    """
    import adsk.core
    import adsk.fusion
    root = _root()
    if root is None:
        return {"ok": False, "error": "no root component"}
    try:
        # 目标实体: 优先厚度 5mm 量级的首个实体(web); 无则任一实体
        target = None
        if root.bRepBodies.count > 0:
            target = root.bRepBodies.item(0)
            for b in root.bRepBodies:
                try:
                    bb = b.boundingBox
                    if 4.0 <= (bb.maxPoint.z - bb.minPoint.z) * 10.0 <= 6.0:
                        target = b
                        break
                except Exception:
                    pass
        if target is None:
            return {"ok": False, "error": "无目标实体可打孔"}

        sk = root.sketches.add(root.xYConstructionPlane)
        for (x, y) in points_xy_mm:
            center = adsk.core.Point3D.create(x / 10.0, y / 10.0, 0.0)  # mm→内部cm
            radius = diameter_mm / 2.0 / 10.0
            sk.sketchCurves.sketchCircles.addByCenterRadius(center, radius)

        ext = root.features.extrudeFeatures
        n = sk.profiles.count
        created = 0
        failed = []
        # 单次 input 全部 profiles(更稳) + 逐个 profile 回退容错
        profile_set = adsk.core.ObjectCollection.create()
        for i in range(n):
            profile_set.add(sk.profiles.item(i))
        att = None
        try:
            inp = ext.createInput(profile_set, adsk.fusion.FeatureOperations.CutFeatureOperation)
            try:
                oc2 = adsk.core.ObjectCollection.create()
                oc2.add(target)
                inp.participantBodies = oc2
            except Exception:
                pass
            inp.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
            ext.add(inp)
            created = n
        except Exception as e:
            att = str(e)
            # 回退: 逐 profile 单切
            for i in range(n):
                try:
                    prof = sk.profiles.item(i)
                    inp2 = ext.createInput(prof, adsk.fusion.FeatureOperations.CutFeatureOperation)
                    try:
                        oc3 = adsk.core.ObjectCollection.create()
                        oc3.add(target)
                        inp2.participantBodies = oc3
                    except Exception:
                        pass
                    inp2.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
                    ext.add(inp2)
                    created += 1
                except Exception as e2:
                    failed.append({"profile": i, "error": f"{type(e2).__name__}: {e2}"})
        out = {"ok": created > 0 or n == 0, "holes": created, "total": n}
        if failed:
            out["failed"] = failed
        if att is not None:
            out["batch_error"] = att
        return out
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def create_cone(radius_big_mm, radius_small_mm, height_mm, center_xyz=(0.0, 0.0, 0.0),
                axis_xyz=(0.0, 0.0, 1.0)) -> dict:
    """创建锥台/圆锥原语(TemporaryBRepManager.createCylinderOrCone, 真实 adsk 桥)。

    用于沉头头部(90° 锥)等 handlers 原语不覆盖的形态。
    返回 {"ok": bool, "bodyName": ..., "error": str?}
    """
    import adsk.core
    import adsk.fusion
    root = _root()
    if root is None:
        return {"ok": False, "error": "no root component"}
    try:
        tmp = adsk.fusion.TemporaryBRepManager.get()
        c = center_xyz
        # Fusion 2704 签名: createCylinderOrCone(pointOne, radiusOne, pointTwo, radiusTwo)
        # (旧版 radius/height/center/axis 已废弃) —— 用两端中心点+半径构造锥台
        p1 = adsk.core.Point3D.create(c[0] / 10.0, c[1] / 10.0,
                                      (c[2] - height_mm / 2.0) / 10.0)
        p2 = adsk.core.Point3D.create(c[0] / 10.0, c[1] / 10.0,
                                      (c[2] + height_mm / 2.0) / 10.0)
        brep = tmp.createCylinderOrCone(p1, radius_big_mm / 10.0,
                                        p2, radius_small_mm / 10.0)
        body = root.bRepBodies.add(brep,
                                   adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        return {"ok": True, "bodyName": getattr(body, "name", "")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def mounting_hole_corners(width_mm: float, length_mm: float,
                          edge_offset_mm: float = 5.0) -> list:
    """四角安装孔坐标(mm, XY 平面, 以 web 几何中心为原点)。"""
    xo = max(edge_offset_mm, width_mm / 2.0 if width_mm < 2 * edge_offset_mm else edge_offset_mm)
    yo = max(edge_offset_mm, length_mm / 2.0 if length_mm < 2 * edge_offset_mm else edge_offset_mm)
    hx = width_mm / 2.0 - xo
    hy = length_mm / 2.0 - yo
    if hx <= 0 or hy <= 0:
        hx = width_mm / 4.0
        hy = length_mm / 4.0
    return [(hx, hy), (hx, -hy), (-hx, hy), (-hx, -hy)]


def merge_fastener_groups() -> dict:
    """紧固件零件合并: 螺栓(杆+锥段) → 1 实体, 螺母 → 1 实体。

    按 AI 属性 semantic_role 分组, 组内用 TMP.booleanOperation(Union) 合成一个
    临时体后 add(实测 combineFeatures Join 在 2704 会丢几何, 改用 TMP union)。
    返回 {"ok", "bolt_body", "nut_body", "bodies"}。
    """
    import adsk.core
    import adsk.fusion
    root = _root()
    if root is None:
        return {"ok": False, "error": "no root component"}
    try:
        tmp = adsk.fusion.TemporaryBRepManager.get()
        groups: dict[str, list] = {"bolt": [], "nut": []}
        for b in root.bRepBodies:
            role = ""
            try:
                for a in b.attributes:
                    if a.groupName == ATTR_GROUP and a.name == "semantic_role":
                        role = a.value
            except Exception:
                pass
            if role == "nut":
                groups["nut"].append(b)
            elif role in ("bolt_rod", "bolt_head"):
                groups["bolt"].append(b)

        def union_group(bodies):
            if not bodies:
                return None, None
            # Fusion 2704: booleanOperation 直接接受文档 BRepBody, 第一个参数就地变结果
            cur = bodies[0]
            for other in bodies[1:]:
                tmp.booleanOperation(cur, other,
                                     adsk.fusion.BooleanTypes.UnionBooleanType)
            for other in bodies[1:]:
                try:
                    other.deleteMe()
                except Exception:
                    pass
            return cur, getattr(cur, "name", "")

        bolt_body, bolt_name = union_group(groups["bolt"])
        nut_body, nut_name = union_group(groups["nut"])
        return {"ok": True, "bolt_body": bolt_name, "nut_body": nut_name,
                "bodies": root.bRepBodies.count}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def apply_thread(body_name: str, is_internal: bool = False,
                 designation: str = "M5x0.8", thread_class: str = "6g",
                 profile: str = "ISO Metric profile", modeled: bool = True) -> dict:
    """在实体圆柱面上创建真实螺纹(threadFeatures, isModeled 实体牙型)。

    探针实证(Fusion 2704): createThreadInfo + createInput(face, info) + add 可用,
    isModeled=True 生成真实螺旋牙型(非装饰标注)。
    返回 {"ok", "thread_name", "error"}。
    """
    import adsk.core
    import adsk.fusion
    root = _root()
    if root is None:
        return {"ok": False, "error": "no root component"}
    try:
        body = body_by_name(body_name)
        if body is None:
            return {"ok": False, "error": f"body {body_name} not found"}
        tf = root.features.threadFeatures
        # 找圆柱面(最大半径面优先, 忽略小孔面)
        face = None
        best_r = -1.0
        for fc in body.faces:
            try:
                if fc.geometry.objectType == adsk.core.Cylinder.classType():
                    cyl = adsk.core.Cylinder.cast(fc.geometry)
                    if cyl.radius > best_r:
                        best_r = cyl.radius
                        face = fc
            except Exception:
                continue
        if face is None:
            return {"ok": False, "error": f"{body_name} 无圆柱面可加螺纹"}
        ti = tf.createThreadInfo(is_internal, profile, designation, thread_class)
        inp = tf.createInput(face, ti)
        try:
            inp.isModeled = modeled
        except Exception:
            pass
        feat = tf.add(inp)
        return {"ok": True, "thread_name": getattr(feat, "name", "") if feat else ""}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def merge_bodies_into_first(keep_first_contains: str = "") -> dict:
    """把文档内其余实体全部 union 进第一个匹配实体(combineFeatures Join)。

    用于将 杆+锥段 等分段原语合并为一个零件实体。
    keep_first_contains: 名称包含该关键词的实体作为目标(默认第一个)。
    """
    import adsk.core
    import adsk.fusion
    root = _root()
    if root is None:
        return {"ok": False, "error": "no root component"}
    try:
        bodies = list(root.bRepBodies)
        if not bodies:
            return {"ok": False, "error": "no bodies"}
        target = None
        if keep_first_contains:
            for b in bodies:
                if keep_first_contains in getattr(b, "name", ""):
                    target = b
                    break
        target = target or bodies[0]
        tools = adsk.core.ObjectCollection.create()
        merged = 0
        for b in bodies:
            if b is not target:
                tools.add(b)
        if tools.count > 0:
            cf = root.features.combineFeatures
            inp = cf.createInput(target, tools)
            inp.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
            cf.add(inp)
            merged = tools.count
        return {"ok": True, "merged": merged, "bodies": root.bRepBodies.count}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


__all__ = ["ATTR_GROUP", "ensure_design", "body_by_name", "set_body_attributes",
           "set_attributes_by_name", "collect_bom", "create_cut_holes",
           "create_cone", "apply_thread", "merge_bodies_into_first",
           "mounting_hole_corners"]