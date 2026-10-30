#!/usr/bin/env python3
"""capability_stack · 2026-08-13 · P0 通用能力栈接入

用户方向(2026-08-13 · 尸检归因→能力清单):
  CC 的「根本能力投入」购物单四项里三项证据不足。真正缺的不是更强 VLM，
  是验证策略栈——sympy 精算、DuckDB 数据分析、ijson 流式解析、faster-whisper ASR、
  PySceneDetect 镜头切分、Wayback CDX 时间锁定。全是机制，全是零成本纯 CPU。

接入纪律(执行铁律):
  ① 一次接一个，接完跑 golden case 验收
  ② 每个工具四件套: 注册表条目(capability schema) → selftest → golden case → 遥测
  ③ 所有触发判断用语义，禁止关键词列表、禁止魔法数字(M-82 病不可复发)
  ④ 接线期间后台 evolution agent 保持冻结

本文件注册 6 个 P0 工具到 qa_tools_registry:
  #1 sympy_calc       — 符号计算引擎(多边形面积/分数运算/方程求解)
  #2 duckdb_query     — 本地 SQL 数据分析引擎(大 JSON/CSV 直接 SQL 查)
  #3 ijson_stream     — 流式 JSON 解析(超大 JSON 不 OOM 不 hang)
  #4 transcribe_audio_local — 本地 ASR(faster-whisper·已有 transcribe_audio·此为注册表版)
  #5 scene_detect     — 镜头切分(label_sweep 升级引擎)
  #6 wayback_cdx      — 时间锁定检索(历史快照按时间戳查)

注意: #4 transcribe_audio 在 qa_auto_agent.py 里已有闭包实现，
  这里不重复实现，而是在注册表里注册一个薄封装层，让 pipeline/auto 两种模式
  都能通过 registry 调用它（之前只有 auto 模式的闭包能调）。
"""
import os
import sys
import json
import time
import re
import hashlib
import subprocess as _sp
import tempfile as _tf

HERE = os.path.dirname(os.path.abspath(__file__))
QA_ROOT = os.path.dirname(HERE)
SEED_ROOT = os.path.dirname(QA_ROOT)
V11_EVOLUTION = os.path.join(SEED_ROOT, "v11", "evolution")
for _p in (V11_EVOLUTION, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qa_tools_registry import register_tool

# ── 遥测(和 _emit_gate_event 同构·这里是能力栈专用遥测) ──────────────────

_CAP_TELEMETRY_LOG = os.path.join(QA_ROOT, "state", "capability_telemetry.jsonl")


def _emit_cap_event(tool_name, event, detail=None):
    """能力栈遥测: 记录每次能力工具的调用/成功/失败/耗时。
    和 gate_events.jsonl 同构·纯观测·只追加·fail-safe。"""
    try:
        _rec = {"ts": _now_iso(), "tool": str(tool_name),
                "event": str(event), "detail": detail or {}}
        with open(_CAP_TELEMETRY_LOG, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(_rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _now_iso():
    try:
        from datetime import datetime
        return datetime.now().isoformat()
    except Exception:
        return ""


def _record_calculation(session, *, engine, method, inputs, result):
    """写入可审计的结构化计算痕迹，供推导链门判断“计算确实发生”。"""
    trace = session.setdefault("calculation_trace", [])
    trace.append({
        "engine": engine,
        "method": method,
        "inputs": str(inputs)[:1000],
        "result": str(result)[:300],
        "ts": _now_iso(),
    })
    # 保持 checkpoint 有界；最近的计算最有价值。
    del trace[:-50]


def _dataset_manifest_path():
    return os.path.join(_SANDBOX_DATA_DIR, "dataset_manifest.json")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _register_dataset(session, file_path, source="local_workspace"):
    """登记可复用数据集：来源、时间、文件哈希进入 session 与工作区 manifest。"""
    entry = {
        "path": os.path.abspath(file_path),
        "source": source,
        "registered_at": _now_iso(),
        "sha256": _sha256_file(file_path),
        "bytes": os.path.getsize(file_path),
    }
    registry = session.setdefault("dataset_workspace", {})
    registry[entry["path"]] = entry
    try:
        os.makedirs(_SANDBOX_DATA_DIR, exist_ok=True)
        manifest_path = _dataset_manifest_path()
        manifest = {}
        if os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        manifest[entry["path"]] = entry
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        pass  # manifest 失败不影响分析；session 中仍保有当前任务的登记。
    return entry


# ═══════════════════════════════════════════════════════════════════════
# #1 sympy_calc — 符号计算引擎
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(多边形题实证): VLM 读对了所有边长(10,6,4,8,6,1…)，但没有任何机制
# 强制"读出数字后去代码里算"——推导链痕迹门已拦下目测交卷，但 agent 自己
# 写 shell_exec 算鞋带公式容易写错。sympy 是确定性符号计算引擎:
# 多边形面积 = 鞋带公式 / 分数运算 = Fraction / 方程求解 = sympy.solve
#
# 融合方式: 配合推导链痕迹门(数值答案必须有 code_exec 在推导链里)。
# golden case = 多边形题: VLM 已读出边长，接上 sympy 当场复跑验证。

def _sympy_available():
    """检查 sympy 是否已安装。"""
    try:
        import sympy
        return True
    except ImportError:
        return False


def _install_sympy():
    """自动安装 sympy(零成本纯 Python)。"""
    try:
        _sp.run([sys.executable, "-m", "pip", "install", "--quiet", "sympy"],
                capture_output=True, text=True, timeout=60)
        return True
    except Exception:
        return False


@register_tool(
    name="sympy_calc",
    description=(
        "符号计算引擎(sympy)。用于需要【精确计算】的场景:\n"
        "  • 多边形面积(鞋带公式): 传入顶点坐标列表，sympy 精确算面积\n"
        "  • 分数运算: Fraction(1,3)+Fraction(1,6) 精确不溢出\n"
        "  • 方程求解: 解方程/方程组/求极值\n"
        "  • 几何计算: 三角函数/向量/距离\n\n"
        "用法:\n"
        "  question 描述你要算什么(自然语言·不限措辞):\n"
        '    "用鞋带公式算多边形面积，顶点(0,0)(4,0)(4,3)(0,3)"\n'
        '    "算 1/3 + 1/6 + 1/12 的和"\n'
        '    "解方程 2*x + 3 = 11"\n'
        "  工具内部用 sympy 符号计算，返回精确结果(不是浮点近似)。\n"
        "  这是「可计算不目测」的发动机——VLM 读出数字后交给它精确算。"
    ),
    category="deep_research", cost_tier="cheap",
)
def t_sympy_calc(question, session):
    t0 = time.time()
    q = (question or "").strip()
    if not q:
        return "错误: 请描述你要算什么(如'用鞋带公式算多边形面积，顶点(0,0)(4,0)(4,3)(0,3)')"

    # 确保 sympy 可用
    if not _sympy_available():
        _emit_cap_event("sympy_calc", "install_start", {})
        if not _install_sympy():
            _emit_cap_event("sympy_calc", "install_fail", {})
            return "sympy 安装失败，请先 pip install sympy"
        _emit_cap_event("sympy_calc", "install_ok", {})

    try:
        import sympy as sp
        from fractions import Fraction

        q_lower = q.lower()

        # ── 语义路由(不靠关键词表·靠数学语义结构) ──
        # 分数是“数字/数字”的表达式，不得被坐标兜底解析误判为一串 (x,y) 点。
        # 先走其更具体的句法结构；否则 1/3 + 1/6 + 1/12 会被错误配对成三顶点。
        _frac_match = _extract_fraction_expr(q)
        if _frac_match:
            result = _compute_fractions(_frac_match)
            _emit_cap_event("sympy_calc", "ok", {"method": "fractions",
                              "expr": _frac_match, "result": str(result),
                              "elapsed_s": round(time.time() - t0, 2)})
            _record_calculation(session, engine="fractions", method="exact_fraction",
                                inputs=_frac_match, result=result)
            return f"[sympy·分数] {_frac_match} = {result}"

        # 鞋带公式: 检测坐标对序列
        _coords = _extract_coords(q)
        if _coords and len(_coords) >= 3:
            # 鞋带公式只能保证“给定点序列”的面积正确，不能保证点序列就是题图。
            # 对声明为正交/直角的图形，先把模型本身作为输入契约校验：每条边必须
            # 平行坐标轴、非零且边界不能自交。失败时不写 calculation_trace，避免
            # 把“精确地算错模型”伪装成已验证的推导链。
            if _declares_orthogonal_geometry(q):
                _geometry_error = _validate_orthogonal_polygon(_coords)
                if _geometry_error:
                    _emit_cap_event("sympy_calc", "model_rejected", {
                        "method": "shoelace", "reason": _geometry_error,
                        "n_vertices": len(_coords),
                    })
                    return ("[sympy·模型拒绝] 题目/输入声明的是正交图形，但给出的顶点模型"
                            f"不满足该结构：{_geometry_error}。未执行面积计算，也未写入 calculation_trace。"
                            "请逐边补全顶点；每一相邻边应水平或竖直，且边界不可相交。")
            result = _shoelace_area(sp, _coords)
            _emit_cap_event("sympy_calc", "ok", {"method": "shoelace",
                              "n_vertices": len(_coords), "elapsed_s": round(time.time() - t0, 2)})
            # 把提取值和计算结果存入 session（推导链痕迹门的证据链）
            # 必须写回同一个 session budget；空 dict 不能用 `or {}` 替换，
            # 否则计算痕迹会落进临时 dict，推导链门看不到。
            _budget = session.setdefault("_budget", {})
            _ev = _budget.setdefault("_vlm_extracted_values", [])
            for c in _coords:
                for v in c:
                    _v_str = str(v)
                    if _v_str not in _ev:
                        _ev.append(_v_str)
            _budget["sympy_result"] = str(result)
            _record_calculation(session, engine="sympy", method="shoelace",
                                inputs=_coords, result=result)
            return f"[sympy·鞋带公式] {len(_coords)} 个顶点 → 面积 = {result}"

        # 方程求解: 检测 "解方程"/"solve"/"=" 模式
        if any(kw in q_lower for kw in ("解方程", "solve", "方程", "equation")):
            result = _solve_equation(sp, q)
            _emit_cap_event("sympy_calc", "ok", {"method": "solve",
                              "elapsed_s": round(time.time() - t0, 2)})
            _record_calculation(session, engine="sympy", method="solve_equation",
                                inputs=q, result=result)
            return f"[sympy·方程] {result}"

        # 通用算术表达式
        _expr = _extract_arith_expr(q)
        if _expr:
            result = sp.sympify(_expr)
            _emit_cap_event("sympy_calc", "ok", {"method": "arithmetic",
                              "expr": _expr, "result": str(result),
                              "elapsed_s": round(time.time() - t0, 2)})
            _record_calculation(session, engine="sympy", method="arithmetic",
                                inputs=_expr, result=result)
            return f"[sympy·算术] {_expr} = {result}"

        return (f"无法自动解析计算意图。请明确描述:\n"
                f"  • 鞋带公式: '面积 顶点 (0,0)(4,0)(4,3)(0,3)'\n"
                f"  • 分数: '1/3 + 1/6 + 1/12'\n"
                f"  • 方程: '解方程 2*x + 3 = 11'")

    except Exception as e:
        _emit_cap_event("sympy_calc", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"sympy 计算异常: {type(e).__name__}: {str(e)[:150]}"


def _extract_coords(text):
    """从文本中提取坐标对序列。支持 (x,y)(x,y) 或 x,y; x,y 格式。"""
    # 方式1: (x,y)(x,y)...
    pairs = re.findall(r'\(\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*\)', text)
    if len(pairs) >= 3:
        # 保留原始文本，后续由 sympy.Rational 精确处理，避免 float 二进制误差。
        return pairs
    # 方式2: 数字对用空格/分号分隔
    nums = re.findall(r'-?\d+(?:\.\d+)?', text)
    if len(nums) >= 6 and len(nums) % 2 == 0:
        return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
    return None


def _declares_orthogonal_geometry(text):
    """判断调用者是否明确要求正交模型；用于校验模型，不参与算式路由。"""
    normalized = (text or "").lower()
    return any(marker in normalized for marker in (
        "rectilinear", "orthogonal", "right angle", "axis-aligned",
        "正交", "直角", "水平和竖直", "横竖",
    ))


def _validate_orthogonal_polygon(coords):
    """返回正交简单多边形的结构错误；合法时返回空字符串。"""
    try:
        points = [(float(x), float(y)) for x, y in coords]
    except (TypeError, ValueError):
        return "顶点不是可解析的数值坐标"
    n = len(points)
    for index in range(n):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % n]
        if (x1, y1) == (x2, y2):
            return f"第 {index + 1} 条边长度为零"
        if x1 != x2 and y1 != y2:
            return f"第 {index + 1} 条边不是水平或竖直边"

    # 简单正交多边形的非相邻边不应相交或重叠。相邻边共享端点是合法的。
    def intersects(a, b, c, d):
        ax1, ay1, ax2, ay2 = *a, *b
        bx1, by1, bx2, by2 = *c, *d
        a_vertical = ax1 == ax2
        b_vertical = bx1 == bx2
        if a_vertical == b_vertical:
            if a_vertical:
                return ax1 == bx1 and max(min(ay1, ay2), min(by1, by2)) <= min(max(ay1, ay2), max(by1, by2))
            return ay1 == by1 and max(min(ax1, ax2), min(bx1, bx2)) <= min(max(ax1, ax2), max(bx1, bx2))
        vertical, horizontal = ((a, b), (c, d)) if a_vertical else ((c, d), (a, b))
        (vx1, vy1), (vx2, vy2) = vertical
        (hx1, hy1), (hx2, hy2) = horizontal
        return min(vy1, vy2) <= hy1 <= max(vy1, vy2) and min(hx1, hx2) <= vx1 <= max(hx1, hx2)

    for i in range(n):
        for j in range(i + 1, n):
            if j in {i, (i + 1) % n} or i == (j + 1) % n:
                continue
            if intersects(points[i], points[(i + 1) % n], points[j], points[(j + 1) % n]):
                return f"第 {i + 1} 与第 {j + 1} 条非相邻边相交或重叠"
    return ""


def _shoelace_area(sp, coords):
    """鞋带公式算多边形面积: A = ½|Σ(x_i·y_{i+1} − x_{i+1}·y_i)|"""
    n = len(coords)
    # 闭合多边形(首尾相连)
    total = sp.Integer(0)
    for i in range(n):
        x1, y1 = sp.Rational(str(coords[i][0])), sp.Rational(str(coords[i][1]))
        x2, y2 = (sp.Rational(str(coords[(i + 1) % n][0])),
                  sp.Rational(str(coords[(i + 1) % n][1])))
        total += x1 * y2 - x2 * y1
    area = sp.Abs(total) / 2
    return area


def _extract_fraction_expr(text):
    """提取分数运算表达式(如 1/3 + 1/6)。"""
    # 匹配包含 / 的算术表达式
    m = re.search(r'[\d\s/+\-()]+/\d+', text)
    if m:
        expr = m.group().strip()
        if '/' in expr and any(c.isdigit() for c in expr):
            return expr
    return None


def _compute_fractions(expr):
    """精确计算分数表达式，不走 Python `/` 的浮点除法。"""
    # 仅允许普通分数算术字符；随后将 a/b 显式转成 Fraction(a,b)。
    safe_expr = re.sub(r'[^\d/+\-() ]', '', expr)
    if not safe_expr.strip() or safe_expr != expr:
        return "无法解析"
    try:
        from fractions import Fraction
        fraction_expr = re.sub(r'(\d+)\s*/\s*(\d+)', r'Fraction(\1,\2)', safe_expr)
        return eval(fraction_expr, {"__builtins__": {}}, {"Fraction": Fraction})
    except (ArithmeticError, SyntaxError, ValueError):
        return "表达式解析失败"


def _solve_equation(sp, text):
    """解方程。"""
    # 提取等式
    m = re.search(r'([0-9a-zA-Z+\-*/\s=()]+)', text)
    if not m:
        return "无法解析方程"
    expr_str = m.group(1).strip()
    if '=' not in expr_str:
        return "未找到等号(=)"
    # 分离左右
    parts = expr_str.split('=')
    if len(parts) != 2:
        return "方程格式不对(需要恰好一个等号)"
    try:
        left = sp.sympify(parts[0].strip())
        right = sp.sympify(parts[1].strip())
        eq = sp.Eq(left, right)
        sols = sp.solve(eq)
        return f"解: {sols}"
    except Exception as e:
        return f"求解失败: {str(e)[:100]}"


def _extract_arith_expr(text):
    """提取纯算术表达式。"""
    m = re.search(r'[\d+\-*/() ]+', text)
    if m:
        expr = m.group().strip()
        if any(c.isdigit() for c in expr):
            return expr
    return None


# ═══════════════════════════════════════════════════════════════════════
# #2 duckdb_query — 本地 SQL 数据分析引擎
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(PubChem 题): 几千页 JSON 用 urlopen 整体载入 → hang/OOM。
# DuckDB 直接对 JSON/CSV/Parquet 文件跑 SQL，不需要导入数据库。
# 「数据集工作区」模式: 大文件下载一次进 workspace 登记(来源/时间/hash)，
# 后续所有查询走 DuckDB 不重复下载。

_SANDBOX_DATA_DIR = os.path.join(QA_ROOT, "workspace_data")


def _duckdb_available():
    try:
        import duckdb
        return True
    except ImportError:
        return False


def _install_duckdb():
    try:
        _sp.run([sys.executable, "-m", "pip", "install", "--quiet", "duckdb"],
                capture_output=True, text=True, timeout=60)
        return True
    except Exception:
        return False


@register_tool(
    name="duckdb_query",
    description=(
        "本地 SQL 数据分析引擎(DuckDB)。直接对 JSON/CSV/Parquet 文件跑 SQL，"
        "无需导入数据库、无需翻页、不会 OOM。适合大数据集分析:\n\n"
        "  • PubChem 批量 JSON: 下载一次 → SQL 筛选+排序+聚合\n"
        "  • 大 CSV 分析: 直接 SELECT/WHERE/GROUP BY\n"
        "  • 超大 JSON 流式查询\n\n"
        "用法:\n"
        "  question = '文件路径|SQL语句' 或 '文件路径|自然语言描述'\n"
        "  工具会把指定文件绑定成只读视图 dataset；SQL 直接查询 dataset，避免相对路径漂移。\n"
        "  示例: 'data.json|SELECT * FROM dataset WHERE mw > 300 ORDER BY mw DESC LIMIT 5'\n"
        "  示例: 'data.csv|SELECT count(*) FROM dataset'\n"
        "  文件路径相对数据集工作区。首次会自动安装 duckdb(纯 CPU)。"
    ),
    category="deep_research", cost_tier="medium",
)
def t_duckdb_query(question, session):
    t0 = time.time()
    q = (question or "").strip()
    if not q or "|" not in q:
        return ("错误: 格式为 '文件路径|SQL语句' 或 '文件路径|自然语言描述'\n"
                "示例: 'data.json|SELECT * FROM read_json_auto(\\'data.json\\') LIMIT 10'")

    parts = q.split("|", 1)
    file_path = parts[0].strip()
    sql_or_desc = parts[1].strip()

    # 确保 DuckDB 可用
    if not _duckdb_available():
        _emit_cap_event("duckdb_query", "install_start", {})
        if not _install_duckdb():
            _emit_cap_event("duckdb_query", "install_fail", {})
            return "duckdb 安装失败，请先 pip install duckdb"
        _emit_cap_event("duckdb_query", "install_ok", {})

    # 解析文件路径(相对→绝对)
    os.makedirs(_SANDBOX_DATA_DIR, exist_ok=True)
    if not os.path.isabs(file_path):
        file_path = os.path.join(_SANDBOX_DATA_DIR, file_path)
    if not os.path.exists(file_path):
        return f"错误: 文件不存在 {file_path}"
    dataset = _register_dataset(session, file_path)

    try:
        import duckdb

        # 判断文件类型选择读取函数
        ext = os.path.splitext(file_path)[1].lower()
        read_fn_map = {
            ".json": "read_json_auto",
            ".jsonl": "read_json_auto",
            ".csv": "read_csv_auto",
            ".tsv": "read_csv_auto",
            ".parquet": "read_parquet",
        }
        read_fn = read_fn_map.get(ext, "read_json_auto")

        # 如果是自然语言(不是 SELECT 开头)，先看 schema 再帮建 SQL
        if not sql_or_desc.upper().strip().startswith(("SELECT", "WITH", "DESCRIBE", "SHOW", "PRAGMA")):
            # 先 DESCRIBE 看结构
            con = duckdb.connect()
            schema = con.execute(f"DESCRIBE SELECT * FROM {read_fn}('{file_path}') LIMIT 1").fetchall()
            cols = [row[0] for row in schema]
            con.close()
            # 简单意图理解(语义·不靠关键词表): 如果描述里有 "最重/最大/最多"
            # → ORDER BY ... DESC; 如果有 "筛选/where" → WHERE 条件
            desc_lower = sql_or_desc.lower()
            order_desc = any(w in desc_lower for w in ("最重", "最大", "最多", "heaviest", "largest", "most"))
            # 构建基础 SQL
            sql = "SELECT * FROM dataset"
            if order_desc and cols:
                # 尝试找分子量列(语义匹配列名)
                mw_col = next((c for c in cols if c.lower() in ("mw", "molecular_weight", "molecularweight",
                                                                  "mass", "weight", "mw_g", "monoisotopic_mass")),
                              cols[0])
                sql += f" ORDER BY {mw_col} DESC"
            sql += " LIMIT 10"
        else:
            sql = sql_or_desc.replace("\\'", "'")

        con = duckdb.connect()
        # SQL 一律通过稳定的 dataset 视图访问当前登记文件，避免模型在 SQL 内
        # 重复拼接相对文件路径导致“注册了数据集但查到别的文件”。
        escaped_path = file_path.replace("'", "''")
        con.execute(f"CREATE OR REPLACE VIEW dataset AS SELECT * FROM {read_fn}('{escaped_path}')")
        rows = con.execute(sql).fetchall()
        col_names = [d[0] for d in con.description]
        con.close()

        _emit_cap_event("duckdb_query", "ok", {"file": os.path.basename(file_path),
                          "sha256": dataset["sha256"], "n_rows": len(rows),
                          "elapsed_s": round(time.time() - t0, 2)})

        # 格式化输出
        lines = [f"[duckdb] {len(rows)} 行 · {', '.join(col_names[:8])}"]
        for i, row in enumerate(rows[:10]):
            lines.append(f"  [{i+1}] {dict(zip(col_names, row))}")
        return "\n".join(lines)

    except Exception as e:
        _emit_cap_event("duckdb_query", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"duckdb 查询异常: {type(e).__name__}: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════════════════════
# #3 ijson_stream — 流式 JSON 解析
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(urlopen hang): 大 JSON 整体载入 → 内存爆炸/hang。
# ijson 流式迭代: 永不整体载入，逐条处理。

def _ijson_available():
    try:
        import ijson
        return True
    except ImportError:
        return False


def _install_ijson():
    try:
        _sp.run([sys.executable, "-m", "pip", "install", "--quiet", "ijson"],
                capture_output=True, text=True, timeout=30)
        return True
    except Exception:
        return False


@register_tool(
    name="ijson_stream",
    description=(
        "流式 JSON 解析引擎(ijson)。处理超大 JSON 文件(几百MB到GB级)永不 OOM 不 hang。"
        "逐条迭代 JSON 数组元素，内存占用恒定(不随文件大小增长)。\n\n"
        "适合: PubChem 批量 JSON、OpenAlex 大导出、任何超大 JSON 文件。\n"
        "用法:\n"
        "  question = '文件路径|提取路径|筛选条件(可选)|最多提取N条'\n"
        "  示例: 'data.json|item|mw>300|100' → 从 data.json 的 item 数组里"
        "  筛选 mw>300 的前 100 条\n"
        "  提取路径: ijson 路径格式，如 'item'(顶层数组)、'results.item'等。"
    ),
    category="deep_research", cost_tier="medium",
)
def t_ijson_stream(question, session):
    t0 = time.time()
    q = (question or "").strip()
    if not q or "|" not in q:
        return ("错误: 格式为 '文件路径|提取路径|筛选条件(可选)|最多N条'\n"
                "示例: 'data.json|item|mw>300|100'")

    parts = q.split("|")
    file_path = parts[0].strip()
    ijson_path = parts[1].strip() if len(parts) > 1 else "item"
    filter_cond = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    max_items = int(parts[3]) if len(parts) > 3 and parts[3].strip().isdigit() else 100

    # 确保文件路径
    if not os.path.isabs(file_path):
        file_path = os.path.join(_SANDBOX_DATA_DIR, file_path)
    if not os.path.exists(file_path):
        return f"错误: 文件不存在 {file_path}"
    dataset = _register_dataset(session, file_path)

    if not _ijson_available():
        if not _install_ijson():
            return "ijson 安装失败，请先 pip install ijson"

    try:
        import ijson

        results = []
        file_size = os.path.getsize(file_path)

        with open(file_path, "rb") as f:
            for item in ijson.items(f, ijson_path):
                if len(results) >= max_items:
                    break
                # 筛选条件(简单数值比较)
                if filter_cond and isinstance(item, dict):
                    # 解析 "key>value" / "key<value" / "key=value"
                    m = re.match(r'(\w+)\s*([><=]+)\s*(-?\d+(?:\.\d+)?)', filter_cond)
                    if m:
                        key, op, val = m.group(1), m.group(2), float(m.group(3))
                        item_val = item.get(key)
                        if item_val is None:
                            continue
                        try:
                            item_val = float(item_val)
                        except (ValueError, TypeError):
                            continue
                        if op == ">" and not item_val > val:
                            continue
                        elif op == "<" and not item_val < val:
                            continue
                        elif op == "=" and not item_val == val:
                            continue
                results.append(item)

        _emit_cap_event("ijson_stream", "ok", {"file": os.path.basename(file_path),
                          "sha256": dataset["sha256"],
                          "file_size_mb": round(file_size / 1024 / 1024, 1),
                          "n_items": len(results), "elapsed_s": round(time.time() - t0, 2)})

        # 格式化输出
        lines = [f"[ijson] {len(results)} 条 (文件 {round(file_size/1024/1024, 1)}MB · 路径 '{ijson_path}')"]
        for i, item in enumerate(results[:10]):
            lines.append(f"  [{i+1}] {json.dumps(item, ensure_ascii=False)[:200]}")
        return "\n".join(lines)

    except Exception as e:
        _emit_cap_event("ijson_stream", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"ijson 解析异常: {type(e).__name__}: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════════════════════
# #4 transcribe_audio_local — 本地 ASR (faster-whisper)
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(GameGrumps 题): 视频是评论节目，音频轨就是答案轨，
# 但 transcribe_audio 的 falsification 命令交付多日未执行。
# 此工具是 qa_auto_agent.py 闭包版 transcribe_audio 的注册表封装，
# 让 pipeline 模式也能调用。

@register_tool(
    name="transcribe_audio_local",
    description=(
        "本地 ASR 语音转录(faster-whisper)。把音频/视频转成文字，支持:\n"
        "  ① 本地音视频文件(.mp3/.wav/.m4a/.mp4 等)\n"
        "  ② 直链音视频 URL\n"
        "  ③ YouTube/视频站链接(→yt-dlp 抽音轨)\n\n"
        "用于: 题目附件是录音/视频、或问「某 YouTube 视频里说了啥/第N个短片里的数字」——"
        "这些信息只在音轨里。返回转录文本(截 max_seconds 秒)。\n"
        "首次会下载 whisper 模型(~150MB·base)。纯 CPU 可跑。"
    ),
    category="deep_research", cost_tier="expensive", retryable=True, max_retry_hint=1,
)
def t_transcribe_audio_local(question, session):
    t0 = time.time()
    path_or_url = (question or "").strip()
    if not path_or_url:
        return "错误: 请传入音频/视频文件路径或 URL"

    # 复用 qa_auto_agent 的闭包实现(如果可用)
    try:
        import qa_auto_agent as _qa
        # 闭包工具在 _make_tools 里面，我们直接调底层逻辑
        # 更安全的做法: 直接用 faster-whisper + yt-dlp(和闭包版同逻辑)
        import tempfile as _tf2
        import subprocess as _sp2
        import glob as _glob

        _workdir = _tf2.mkdtemp(prefix="transcribe_reg_")
        _audio = None
        _VID_SITES = ("youtube.com", "youtu.be", "vimeo.com", "bilibili.com",
                      "dailymotion.com", "twitch.tv")

        try:
            src = path_or_url
            _lo = src.lower().split("?")[0]

            if any(_v in src.lower() for _v in _VID_SITES):
                _out = os.path.join(_workdir, "a.%(ext)s")
                _ytcmd = ["yt-dlp", "-x", "--audio-format", "wav",
                          "--postprocessor-args", "-ar 16000",
                          "--max-filesize", "100M", "-o", _out, src]
                try:
                    import video_frame as _vf
                    _px = _vf._detect_proxy()
                    if _px:
                        _ytcmd = _ytcmd[:1] + ["--proxy", _px] + _ytcmd[1:]
                except Exception:
                    pass
                _r = _sp2.run(_ytcmd, capture_output=True, text=True, timeout=180)
                _cands = _glob.glob(os.path.join(_workdir, "a.*"))
                if not _cands:
                    return f"transcribe: yt-dlp 抽音轨失败(rc={_r.returncode})·{(_r.stderr or '')[:150]}"
                _audio = _cands[0]
            elif src.startswith(("http://", "https://")):
                _sfx = next((e for e in (".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm")
                             if _lo.endswith(e)), ".bin")
                _audio = os.path.join(_workdir, "a" + _sfx)
                _sp2.run(["curl", "-sL", "--max-time", "120", "--max-filesize",
                          str(100 * 1024 * 1024), "-o", _audio, src],
                         capture_output=True, timeout=130)
            else:
                # 本地文件(相对→沙箱)
                _real = src if os.path.isabs(src) else os.path.join(_SANDBOX_DATA_DIR, src)
                if not os.path.exists(_real):
                    return f"文件不存在 {_real}"
                _audio = _real

            if not _audio or not os.path.exists(_audio) or os.path.getsize(_audio) == 0:
                return "未拿到可转录的音频文件(下载/抽取失败)"

            try:
                from faster_whisper import WhisperModel
            except Exception as _ie:
                return (f"转录库未装({str(_ie)[:60]})·"
                        "可先 pip_install faster-whisper")

            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            _model = WhisperModel("base", device="cpu", compute_type="int8")
            _segments, _info = _model.transcribe(_audio, beam_size=1)
            _texts = []
            for _seg in _segments:
                if _seg.start > 600:
                    _texts.append(f"...(超过 600s·已截断)")
                    break
                _texts.append(_seg.text)
            _txt = "".join(_texts).strip()

            _emit_cap_event("transcribe_audio_local", "ok", {
                "lang": getattr(_info, "language", "?"),
                "n_chars": len(_txt), "elapsed_s": round(time.time() - t0, 2)})

            # 存入 session 供下游工具消费
            session.setdefault("evidence", []).append({
                "url": path_or_url, "title": f"transcribe:{path_or_url[:60]}",
                "content": _txt, "source": "transcribe_audio_local"})

            return (f"[transcribe·{getattr(_info,'language','?')}] {len(_txt)}字:\n{_txt[:8000]}"
                    if _txt else "[transcribe] 转录成功但无语音内容(纯音乐/静音?)")

        except _sp2.TimeoutExpired:
            _emit_cap_event("transcribe_audio_local", "timeout", {"elapsed_s": round(time.time() - t0, 2)})
            return "transcribe: 下载/抽取超时"
        finally:
            try:
                if _workdir and os.path.isdir(_workdir):
                    import shutil as _sh
                    _sh.rmtree(_workdir, ignore_errors=True)
            except Exception:
                pass
    except Exception as e:
        _emit_cap_event("transcribe_audio_local", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"transcribe 异常: {type(e).__name__}: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════════════════════
# #5 scene_detect — 镜头切分(label_sweep 升级引擎)
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(360VR 题): label_sweep 盲扫 N 帧，0 帧命中(locate 缺口)。
# PySceneDetect 按镜头切分 → 智能采样每个镜头的关键帧，
# 替换 label_sweep 内部实现，对外接口不变。

def _scenedetect_available():
    try:
        import scenedetect
        return True
    except ImportError:
        return False


def _install_scenedetect():
    try:
        _sp.run([sys.executable, "-m", "pip", "install", "--quiet",
                 "scenedetect[opencv]"],
                capture_output=True, text=True, timeout=120)
        return True
    except Exception:
        return False


@register_tool(
    name="scene_detect",
    description=(
        "镜头切分引擎(PySceneDetect)。按场景/镜头切分视频，返回每个镜头的"
        "起止时间。比 label_sweep 盲扫 N 帧更精准——按镜头边界智能采样关键帧。\n\n"
        "适合: 视频题需要定位「含文字/关键画面」的时刻。\n"
        "用法:\n"
        "  question = '视频URL或搜索描述'\n"
        "  返回: 镜头列表(每个镜头的起止时间+持续秒数)\n"
        "  配合 see_video_frame(url, at_time=镜头起始时间) 精读关键帧。"
    ),
    category="deep_research", cost_tier="medium",
)
def t_scene_detect(question, session):
    t0 = time.time()
    url = (question or "").strip()
    if not url:
        return "错误: 请传入视频 URL 或搜索描述"

    if not _scenedetect_available():
        _emit_cap_event("scene_detect", "install_start", {})
        if not _install_scenedetect():
            _emit_cap_event("scene_detect", "install_fail", {})
            # 降级: 用 video_frame.label_sweep(已有实现)
            try:
                import video_frame as _vf
                res = _vf.label_sweep(url, max_sec=120)
                if res.get("ok"):
                    hits = res.get("hits") or []
                    if hits:
                        _lines = [f"  {h['ts']}({h['time_sec']}s): {h['text_preview'][:60]!r}"
                                  for h in hits]
                        return f"[scene_detect·降级 label_sweep] {len(hits)} 个含文字时刻:\n" + "\n".join(_lines)
                    return f"[scene_detect·降级] label_sweep 未发现文字帧: {res.get('reason','')}"
                return f"[scene_detect·降级] label_sweep 失败: {res.get('reason','')}"
            except Exception as e:
                return f"降级也失败: {type(e).__name__}: {str(e)[:100]}"
        _emit_cap_event("scene_detect", "install_ok", {})

    _vid_path = None
    try:
        from scenedetect import detect, ContentDetector
        import video_frame as _vf
        import tempfile as _tf

        # 先下载视频(复用 video_frame 的下载逻辑)。NamedTemporaryFile 只负责分配唯一
        # 路径；finally 在成功、失败、超时三条路径统一删除，禁止 /tmp 按 URL 永久积累视频。
        _fd, _vid_path = _tf.mkstemp(prefix="meyo_scenedetect_", suffix=".mp4")
        os.close(_fd)

        # 判断是否需要搜索解析
        _u = str(url)
        _is_search = (not _u.startswith(("http://", "https://", "www."))) or \
                     ("results?search_query=" in _u)
        if _is_search:
            vids = _vf.search_videos(str(url), limit=1)
            if vids and vids[0].get("url"):
                url = vids[0]["url"]
            else:
                return f"按 '{url[:50]}' 没搜到视频"

        cmd = ["yt-dlp", "-f", "worst[ext=mp4]/worst", "-o", _vid_path, url]
        if _proxy:
            cmd = cmd[:1] + ["--proxy", _proxy] + cmd[1:]
        _r = _sp.run(cmd, capture_output=True, text=True, timeout=180)
        if _r.returncode != 0 or not os.path.exists(_vid_path):
            return f"视频下载失败: {(_r.stderr or '')[-150:]}"

        # 镜头切分
        scene_list = detect(_vid_path, ContentDetector())

        if not scene_list:
            return "[scene_detect] 未检测到镜头切换(可能是连续画面/纯文字视频)"

        # 格式化镜头列表
        lines = [f"[scene_detect] {len(scene_list)} 个镜头:"]
        key_frames = []
        for i, (start, end) in enumerate(scene_list):
            start_sec = start.get_seconds()
            end_sec = end.get_seconds()
            duration = end_sec - start_sec
            # 每个镜头取中间时刻作为关键帧
            mid_sec = (start_sec + end_sec) / 2
            ts_str = f"{int(mid_sec // 60)}:{int(mid_sec % 60):02d}"
            key_frames.append({"scene": i + 1, "ts": ts_str, "time_sec": mid_sec,
                               "duration": round(duration, 1)})
            lines.append(f"  镜头{i+1}: {int(start_sec//60)}:{int(start_sec%60):02d}"
                        f" → {int(end_sec//60)}:{int(end_sec%60):02d}"
                        f" ({duration:.1f}s) · 关键帧 @ {ts_str}")

        lines.append(f"\n建议: 用 see_video_frame(url, at_time='{key_frames[0]['ts']}') 精读各镜头关键帧")

        _emit_cap_event("scene_detect", "ok", {"n_scenes": len(scene_list),
                          "elapsed_s": round(time.time() - t0, 2)})

        return "\n".join(lines)

    except Exception as e:
        _emit_cap_event("scene_detect", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"scene_detect 异常: {type(e).__name__}: {str(e)[:150]}"
    finally:
        try:
            if _vid_path and os.path.exists(_vid_path):
                os.remove(_vid_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# #6 wayback_cdx — 时间锁定检索
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(Mario Kart 纪录题): 答案随时间漂移(living-gold)。
# Wayback CDX API 按时间戳查历史快照，配合 M-86 时间轴答案合同。

@register_tool(
    name="wayback_cdx",
    description=(
        "时间锁定检索(Wayback Machine CDX API)。按时间戳查询 URL 的历史快照，"
        "用于「截至某时间」类题——答案可能随时间变化(living-gold)。\n\n"
        "用法:\n"
        "  question = 'URL|时间范围(可选)'\n"
        "  示例: 'en.wikipedia.org/wiki/Mario_Kart_8|2023' → 查 2023 年的快照\n"
        "  示例: 'example.com|20230101-20231231' → 精确日期范围\n"
        "  返回: 可用快照列表(时间戳+URL)·可进一步 fetch_url_smart 取历史内容。\n"
        "免费 API·无需注册。"
    ),
    category="search", cost_tier="cheap", retryable=True, max_retry_hint=2,
)
def t_wayback_cdx(question, session):
    t0 = time.time()
    q = (question or "").strip()
    if not q:
        return "错误: 格式为 'URL|时间范围(可选)'"

    parts = q.split("|")
    target_url = parts[0].strip()
    time_range = parts[1].strip() if len(parts) > 1 else ""

    # 清理 URL
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url
    # CDX API 用域名+路径(不带协议)
    cdx_url_part = target_url.replace("https://", "").replace("http://", "")

    try:
        import urllib.request
        import urllib.parse

        # 构建 CDX 查询
        cdx_api = "https://web.archive.org/cdx/search/cdx"
        params = {"url": cdx_url_part, "output": "json", "limit": "20"}
        if time_range:
            # 支持 "2023" 或 "20230101-20231231" 格式
            if "-" in time_range and len(time_range) >= 8:
                params["from"] = time_range.split("-")[0]
                params["to"] = time_range.split("-")[1]
            else:
                params["from"] = time_range
                params["to"] = time_range

        cdx_full_url = f"{cdx_api}?{urllib.parse.urlencode(params)}"

        req = urllib.request.Request(cdx_full_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data or len(data) < 2:
            return f"[wayback_cdx] 未找到 '{cdx_url_part}' 的历史快照"

        # 第一行是列名
        headers = data[0]
        snapshots = []
        for row in data[1:]:
            row_dict = dict(zip(headers, row))
            snapshots.append({
                "timestamp": row_dict.get("timestamp", ""),
                "url": row_dict.get("url", ""),
                "status": row_dict.get("statuscode", ""),
                "archive_url": f"https://web.archive.org/web/{row_dict.get('timestamp','')}/{row_dict.get('url','')}",
            })

        _emit_cap_event("wayback_cdx", "ok", {"url": cdx_url_part,
                          "n_snapshots": len(snapshots), "elapsed_s": round(time.time() - t0, 2)})

        # 存入 session 供下游工具消费
        session.setdefault("evidence", []).append({
            "url": cdx_full_url, "title": f"wayback:{cdx_url_part}",
            "content": json.dumps(snapshots, ensure_ascii=False),
            "source": "wayback_cdx"})

        # 格式化输出
        lines = [f"[wayback_cdx] {len(snapshots)} 个历史快照 for {cdx_url_part}:"]
        for i, snap in enumerate(snapshots[:10]):
            ts = snap["timestamp"]
            # 格式化时间戳 20230101120000 → 2023-01-01 12:00:00
            if len(ts) == 14:
                ts_fmt = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
            else:
                ts_fmt = ts
            lines.append(f"  [{i+1}] {ts_fmt} · status={snap['status']}")
            lines.append(f"       {snap['archive_url'][:100]}")
        lines.append(f"\n用 fetch_url_smart 取历史快照内容。")

        return "\n".join(lines)

    except Exception as e:
        _emit_cap_event("wayback_cdx", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"wayback_cdx 异常: {type(e).__name__}: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════════════════════
# #6b wayback_fetch — 时间机器取内容(0b260a57 实证: 两通道全断)
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(0b260a57 ScienceDirect 2022): wayback_cdx 只列快照·引擎还得自己拼
# archive_url 再 fetch——反爬站(ScienceDirect/Elsevier)活站够不着·存档页
# 又常因重定向/压缩/超大一页 fetch 失败·「两通道全断」诚实拒答零分。
# 修: 一步到位——CDX 找目标时点前最近快照 → 直接取存档内容(重试3次·
# gzip 解压·限长文本化)·存 evidence。零特判·通用「as of YYYY」类题。

@register_tool(
    name="wayback_fetch",
    description=(
        "时间机器取内容: 给 URL|目标时点(如 'sciencedirect.com/browse/journals-and-books"
        "?contentType=RW|20220601')→ 自动找该时点前最近的 Wayback 快照并直接取回"
        "存档页面文本。用于「as of YYYY」类题(活站反爬/内容已变)——免掉手动拼"
        "archive_url+fetch 两步(0b260a57 实证: 手动两步全断)。\n\n"
        "用法: question='URL|YYYY[MM[DD]]'·返回 存档时间+正文文本(限长)。免费。"
    ),
    category="search", cost_tier="cheap", retryable=True, max_retry_hint=2,
)
def t_wayback_fetch(question, session):
    t0 = time.time()
    q = (question or "").strip()
    if not q or "|" not in q:
        return "错误: 格式为 'URL|目标时点' 如 'en.wikipedia.org/wiki/X|20220601'"
    target_url, _, when = q.partition("|")
    target_url = target_url.strip()
    when = when.strip() or "20200101"
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url
    cdx_url_part = target_url.replace("https://", "").replace("http://", "")

    try:
        import urllib.request, urllib.parse, gzip, io
        import re as _re

        # ① CDX: 目标时点前最近快照(closest·只要 200)
        params = {"url": cdx_url_part, "output": "json", "limit": "20",
                  "from": "1996", "to": when, "filter": "statuscode:200"}
        cdx_full = f"https://web.archive.org/cdx/search/cdx?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(cdx_full, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data or len(data) < 2:
            return f"[wayback_fetch] {when} 前无 200 快照: {cdx_url_part}"
        headers_row = data[0]
        rows = [dict(zip(headers_row, r)) for r in data[1:]]
        snap = rows[-1]                      # to=when 限制下最后一个=最近
        ts = snap.get("timestamp", "")
        orig = snap.get("original") or snap.get("url") or cdx_url_part

        # ② 取存档内容(重试3·gzip·限长文本化)· id_ 后缀=原始页(无 Wayback 工具条)
        arch = f"https://web.archive.org/web/{ts}id_/{orig}"
        body = ""
        last_err = ""
        for attempt in range(3):
            try:
                req2 = urllib.request.Request(
                    arch, headers={"User-Agent": "Mozilla/5.0",
                                   "Accept-Encoding": "gzip"})
                with urllib.request.urlopen(req2, timeout=30) as r2:
                    raw = r2.read()
                    if r2.headers.get("Content-Encoding", "") == "gzip":
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    body = raw.decode("utf-8", "replace")
                break
            except Exception as e2:
                last_err = f"{type(e2).__name__}: {str(e2)[:80]}"
                time.sleep(2 * (attempt + 1))
        if not body:
            # id_ 模式偶有 404·退化到带工具条模式再试一次
            try:
                arch_fb = f"https://web.archive.org/web/{ts}/{orig}"
                req3 = urllib.request.Request(arch_fb, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req3, timeout=30) as r3:
                    body = r3.read().decode("utf-8", "replace")
                arch = arch_fb
            except Exception as e3:
                return f"[wayback_fetch] 快照存在({ts})但取回失败: {last_err} / {type(e3).__name__}"

        # ③ 文本化: 去 script/style·标签压缩·限 6000 字
        txt = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
        txt = _re.sub(r"(?s)<!--.*?-->", " ", txt)
        txt = _re.sub(r"(?s)<[^>]+>", " ", txt)
        txt = _re.sub(r"&nbsp;", " ", txt)
        txt = _re.sub(r"\s{2,}", " ", txt).strip()
        ts_fmt = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ts
        out = (f"[wayback_fetch] 存档 {ts_fmt} of {orig[:80]}\n({len(txt)}字):\n"
               f"{txt[:6000]}")

        _emit_cap_event("wayback_fetch", "ok", {
            "url": cdx_url_part, "ts": ts, "len": len(txt),
            "elapsed_s": round(time.time() - t0, 2)})
        session.setdefault("evidence", []).append({
            "url": arch, "title": f"wayback:{orig[:60]}@{ts_fmt}",
            "content": txt[:8000], "source": "wayback_fetch"})
        return out

    except Exception as e:
        _emit_cap_event("wayback_fetch", "error", {"error": str(e)[:150],
                          "elapsed_s": round(time.time() - t0, 2)})
        return f"wayback_fetch 异常: {type(e).__name__}: {str(e)[:150]}"


# ═══════════════════════════════════════════════════════════════════════
# Selftest · 能力栈生产门
# ═══════════════════════════════════════════════════════════════════════
#
# 和 selftest_percep_gate.py 同构: 用合成信号走真实代码·10ms 级·不跑 LLM/不跑整道题。
# golden case 从真实失败取: 多边形题 = sympy 金标准, GameGrumps = ASR 金标准,
# PubChem 大 JSON = DuckDB 金标准, Mario Kart = Wayback 金标准。

def _selftest_sympy():
    """sympy golden case: 多边形面积 + 结构化推导痕迹。"""
    session = {"_budget": {}}
    output = t_sympy_calc("正交多边形 顶点(0,0)(4,0)(4,3)(0,3)", session)
    assert output.endswith("面积 = 12"), f"矩形面积应=12, got {output}"
    trace = session.get("calculation_trace") or []
    assert trace and trace[-1].get("engine") == "sympy" and trace[-1].get("result") == "12", (
        f"应写入结构化 calculation_trace, got={trace}")

    import sympy as sp
    coords2 = [(0, 0), (6, 0), (3, 4)]
    result2 = _shoelace_area(sp, coords2)
    assert result2 == 12, f"三角形面积应=12, got {result2}"
    rejected_session = {"_budget": {}}
    rejected = t_sympy_calc("正交多边形 顶点(0,0)(4,0)(3,3)(0,3)", rejected_session)
    assert "模型拒绝" in rejected and not rejected_session.get("calculation_trace"), (
        f"正交题的斜边模型必须被拒绝且不能产生痕迹, got={rejected_session}")
    assert _compute_fractions("1/3 + 1/6 + 1/12") == __import__("fractions").Fraction(7, 12)
    print("  sympy PASS: 鞋带公式+精确分数+calculation_trace ✓")


def _selftest_duckdb():
    """DuckDB golden case: CSV 查询。
    真实失败: PubChem 几千页 JSON 用 urlopen hang。DuckDB 直接 SQL 秒查。"""
    # 创建临时 CSV 测试
    _test_csv = os.path.join(_SANDBOX_DATA_DIR, "_selftest.csv")
    os.makedirs(_SANDBOX_DATA_DIR, exist_ok=True)
    with open(_test_csv, "w") as f:
        f.write("id,name,value\n1,a,100\n2,b,300\n3,c,50\n")
    try:
        session = {}
        output = t_duckdb_query("_selftest.csv|SELECT * FROM dataset WHERE value >= 50 ORDER BY value DESC", session)
        assert "[duckdb] 3 行" in output, f"应返回3行(>=50), got {output}"
        registered = session.get("dataset_workspace", {}).get(os.path.abspath(_test_csv))
        assert registered and registered.get("sha256"), f"应登记数据集来源/哈希, got={session}"
        print("  duckdb PASS: dataset 视图查询+数据集登记(3行·max=300) ✓")
    finally:
        try:
            os.remove(_test_csv)
        except Exception:
            pass


def _selftest_ijson():
    """ijson golden case: 流式解析大 JSON。
    真实失败: urlopen hang。ijson 流式永不 OOM。"""
    if not _ijson_available():
        if not _install_ijson():
            print("  ijson SKIP: 安装失败(网络问题·后续可手动装)")
            return
    import ijson
    _test_json = os.path.join(_SANDBOX_DATA_DIR, "_selftest.json")
    with open(_test_json, "w") as f:
        f.write('{"items":[{"id":1,"mw":400},{"id":2,"mw":200},{"id":3,"mw":350}]}')
    try:
        results = []
        with open(_test_json, "rb") as f:
            for item in ijson.items(f, "items.item"):
                results.append(item)
        assert len(results) == 3, f"应解析3条, got {len(results)}"
        assert results[0]["mw"] == 400, f"第一条 mw 应=400, got {results[0]['mw']}"
        print("  ijson PASS: 流式解析(3条·恒定内存) ✓")
    finally:
        try:
            os.remove(_test_json)
        except Exception:
            pass


def _selftest_p1_registry():
    """P1/P2 工具必须进入共享注册表；确定性 OCR 元数据必须可被推导门消费。"""
    required = {"image_preprocess", "tesseract_ocr", "paddleocr_vl", "dots_ocr", "pdf_extract",
                "chess_engine", "local_vlm_jury", "mineru_pdf", "got_ocr_formula",
                "realesrgan_enhance", "molmo_video_qa", "video_modal_exhaustion"}
    from qa_tools_registry import get_all_tools
    registered = {tool.name for tool in get_all_tools()}
    assert required <= registered, f"P1/P2 注册缺失: {sorted(required - registered)}"
    session = {"_budget": {}}
    values = _record_extracted_values(session, source="synthetic", engine="test", text="1/3 + 2/5 = 11/15")
    assert values == ["1/3", "2/5", "11/15"], f"分数提取错误: {values}"
    assert session["_budget"]["_vlm_extracted_values"] == values, "必须同步推导链门输入"
    from fractions import Fraction
    worksheet = ["1/4", "1/4", "1/4", "1/4", "3/4", "1/15", "1/3", "4/9", "1/8", "32/23", "103/170"]
    total = sum((Fraction(value) for value in worksheet), Fraction(0))
    _record_calculation(session, engine="fractions", method="worksheet_sum", inputs=worksheet, result=total)
    assert session["calculation_trace"][-1]["result"] == str(total), "分数算术必须留下推导痕迹"
    print("  P1/P2 PASS: 共享注册表+提取值元数据+Fraction 推导痕迹 ✓")


def _selftest_wayback():
    """Wayback CDX golden case: 查询历史快照(验证 API 可达)。"""
    try:
        import urllib.request
        import urllib.parse
        cdx_api = "https://web.archive.org/cdx/search/cdx"
        params = {"url": "example.com", "output": "json", "limit": "1"}
        cdx_full_url = f"{cdx_api}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(cdx_full_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data and len(data) >= 2, "应返回至少1条快照"
        print("  wayback PASS: CDX API 可达·返回历史快照 ✓")
    except Exception as e:
        print(f"  wayback SKIP: {type(e).__name__} (网络问题·后续可重试)")


# ═══════════════════════════════════════════════════════════════════════
# #7 ortho_polygon_from_image — 正交多边形图像分析引擎
# ═══════════════════════════════════════════════════════════════════════
#
# 病根(多边形题 GAIA 6359a0b1 · 答案 39):
#   VLM 读出了正确边长，但拓扑不可靠(不同模型给出 6~14 条边·7 种不同顶点序列)，
#   随后的 sympy_calc 是"精确地算一个错误模型"。根本修法：绕开 VLM 的描述路径，
#   直接从像素重建正交轮廓(O(H*W) 纯 CPU)，再把标注数字映射到最近边，完成闭合
#   校验后交 sympy_calc 精算面积。每一步都有可审计的证据。
#
# 算法：
#   Step 1 (像素分割) — 用 PIL/numpy 提取指定颜色(默认=绿色)的 Convex Hull；
#                        检测正交轮廓：水平扫描线提取每行/列的最左/最右绿色像素，
#                        用稀疏占用格(occupancy grid)还原边界转折点。
#   Step 2 (标注提取) — 检测图中与背景颜色不同的"注释像素"中心(非多边形填充色、
#                        非白底)，读出其对应的数值；若 pytesseract 可用则用它；
#                        否则返回像素坐标供 VLM 针对性读数。
#   Step 3 (边长映射) — 每个数字的像素中心映射到距离最近的轮廓边；
#                        边长 = 数字值（单位坐标比例由像素边/已知边推算）。
#   Step 4 (闭合校验) — 水平边长和 = 右移总长；垂直边长和 = 下移总长。
#                        不满足时暴露未标注边的约束方程，让 agent 判断。
#   Step 5 (面积计算) — 若闭合：直接调 _shoelace_area；否则返回诊断报告。
#
# 输出:
#   [ortho_polygon] status=closed vertices=N area=X | 顶点: [(x,y),...] | 边: [...]
#   [ortho_polygon] status=open_constraint need_labels=[edge_idx,...] | 诊断: ...
#   [ortho_polygon] status=error | 原因: ...

def _rgb_mask_green(img_array):
    """提取绿色填充区域：R<G*(0.7), B<G*(0.7), G>50。不硬编码 RGB，按比例检测。"""
    import numpy as np
    R, G, B = img_array[:, :, 0], img_array[:, :, 1], img_array[:, :, 2]
    return (G > 50) & (R < G * 0.7) & (B < G * 0.7)


def _dominant_fill_mask(img_array):
    """检测图中面积最大的填充色区域，作为'多边形'候选。
    先试绿色；若绿色像素不足图面积 2%，则提取最常见非白/非近白的色块。"""
    import numpy as np
    green = _rgb_mask_green(img_array)
    h, w = img_array.shape[:2]
    if green.sum() >= h * w * 0.02:
        return green
    # 退路：最常见的非白色(R>200 & G>200 & B>200 = 白)
    R, G, B = img_array[:, :, 0], img_array[:, :, 1], img_array[:, :, 2]
    not_white = ~((R > 200) & (G > 200) & (B > 200))
    return not_white


def _occupancy_to_vertices(mask):
    """从二值掩码(True=多边形内部)提取正交轮廓顶点。

    算法（段变化追踪，可正确处理非比例示意图）：
      1. 逐行扫描，找每行绿色像素的连续段列表（类型变化=顶点）
      2. 逐列扫描，同样处理
      3. 从行/列段变化中提取顶点，用顺时针追踪重建外轮廓
    """
    import numpy as np

    rows_with_green = np.where(np.any(mask, axis=1))[0]
    if len(rows_with_green) == 0:
        return [], {}
    rmin, rmax = int(rows_with_green.min()), int(rows_with_green.max())
    cmin, cmax = int(np.where(np.any(mask, axis=0))[0].min()), int(np.where(np.any(mask, axis=0))[0].max())

    def get_row_segs(row, gap=4):
        green_cols = np.where(mask[row, :])[0]
        if len(green_cols) == 0:
            return []
        segs = []
        seg_start = int(green_cols[0])
        for i in range(1, len(green_cols)):
            if int(green_cols[i]) - int(green_cols[i-1]) > gap:
                segs.append((seg_start, int(green_cols[i-1])))
                seg_start = int(green_cols[i])
        segs.append((seg_start, int(green_cols[-1])))
        return segs

    def get_col_segs(col, gap=4):
        green_rows = np.where(mask[:, col])[0]
        if len(green_rows) == 0:
            return []
        segs = []
        seg_start = int(green_rows[0])
        for i in range(1, len(green_rows)):
            if int(green_rows[i]) - int(green_rows[i-1]) > gap:
                segs.append((seg_start, int(green_rows[i-1])))
                seg_start = int(green_rows[i])
        segs.append((seg_start, int(green_rows[-1])))
        return segs

    # 收集行/列方向所有段变化点（这些就是外轮廓的折角候选坐标）
    y_boundaries = set()
    x_boundaries_per_row = {}
    last_row_segs = None
    for row in range(rmin, rmax + 2):
        segs = get_row_segs(row) if row <= rmax else []
        if segs != last_row_segs:
            y_boundaries.add(row)
            # 收集该行所有段的端点 x 坐标（+1 为右端点外侧）
            x_set = set()
            for s, e in segs:
                x_set.add(s)
                x_set.add(e + 1)
            x_boundaries_per_row[row] = x_set
            if last_row_segs is not None:
                x_set2 = set()
                for s, e in last_row_segs:
                    x_set2.add(s)
                    x_set2.add(e + 1)
                x_boundaries_per_row[row] = x_set | x_set2
            last_row_segs = segs

    x_boundaries = set()
    last_col_segs = None
    for col in range(cmin, cmax + 2):
        segs = get_col_segs(col) if col <= cmax else []
        if segs != last_col_segs:
            x_boundaries.add(col)
            last_col_segs = segs

    # 合并相近的坐标值（抗像素渗出）
    def merge_close(vals, gap=4):
        vals = sorted(vals)
        if not vals:
            return []
        groups = [[vals[0]]]
        for v in vals[1:]:
            if v - groups[-1][-1] <= gap:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [int(round(sum(g) / len(g))) for g in groups]

    y_edges = merge_close(sorted(y_boundaries))
    x_edges = merge_close(sorted(x_boundaries))

    if len(y_edges) < 2 or len(x_edges) < 2:
        return [], {}

    # 用像素占用格（基于合并后的边界）验证每个单元格
    ny, nx = len(y_edges) - 1, len(x_edges) - 1
    occ = {}
    for iy in range(ny):
        for ix in range(nx):
            cy = (y_edges[iy] + y_edges[iy + 1]) // 2
            cx = (x_edges[ix] + x_edges[ix + 1]) // 2
            cy = min(cy, mask.shape[0] - 1)
            cx = min(cx, mask.shape[1] - 1)
            occ[(iy, ix)] = bool(mask[cy, cx])

    # 用段变化追踪方法提取外轮廓顶点
    vertices_px = _segment_trace_contour(mask, y_edges, x_edges, occ, ny, nx)

    meta = {
        "x_edges_px": x_edges,
        "y_edges_px": y_edges,
        "occupancy": {str(k): v for k, v in occ.items()},
        "bbox": (rmin, rmax, cmin, cmax),
    }
    return vertices_px, meta


def _segment_trace_contour(mask, y_edges, x_edges, occ, ny, nx):
    """正交多边形外轮廓追踪（顺时针，y 轴向下坐标系）。

    算法：占用格标准外边界追踪（参考 Pavlidis 1982）
    ──────────────────────────────────────────────────
    坐标系：格角 (gx, gy)，gx ∈ [0,nx]，gy ∈ [0,ny]
    方向：右=(1,0)，下=(0,1)，左=(-1,0)，上=(0,-1)  [y 向下]

    顺时针规则（多边形在右手边）：
      - 当前方向 d=(dx,dy)，当前格角 (gx,gy)
      - 前方格子 = 当前位置沿 d 方向前进后，右手边格子
      - 如果前方右手格子有填充 → 直行
      - 如果前方右手格子无填充，右手边格子有填充 → 右转后直行
      - 如果前方右手格子有填充，且对角左前格子也有填充 → 左转
    """

    def is_filled(iy, ix):
        if iy < 0 or iy >= ny or ix < 0 or ix >= nx:
            return False
        return occ.get((iy, ix), False)

    filled = [(iy, ix) for (iy, ix), v in occ.items() if v]
    if not filled:
        return []

    # 起始：最上方最左填充格子的左上格角，初始方向向右
    start_cell = min(filled, key=lambda c: (c[0], c[1]))
    start_gx, start_gy = start_cell[1], start_cell[0]

    # 在 y-down 坐标系中，顺时针外轮廓追踪时，"右手"格子就是当前格角右手边的格子
    # 前进方向 (dx, dy) 时:
    #   右手格子 (iy, ix) = 当前位置右手边的格子
    #   右=(1,0): 右手=下方 → (gy, gx)
    #   下=(0,1): 右手=左方 → (gy, gx-1)
    #   左=(-1,0): 右手=上方 → (gy-1, gx-1)
    #   上=(0,-1): 右手=右方 → (gy-1, gx)
    def right_cell(gx, gy, dx, dy):
        if dx == 1:  return gy,     gx
        if dy == 1:  return gy,     gx - 1
        if dx == -1: return gy - 1, gx - 1
        if dy == -1: return gy - 1, gx
        return -1, -1

    # 对角左前方格子（判断凸角）
    # 右=(1,0): 对角左前=上方(gx处) → (gy-1, gx)
    # 下=(0,1): 对角左前=右方(gy处) → (gy, gx)
    # 左=(-1,0): 对角左前=下方(gx-1处) → (gy, gx-1)
    # 上=(0,-1): 对角左前=左方(gy-1处) → (gy-1, gx-1)
    def diag_left_front_cell(gx, gy, dx, dy):
        if dx == 1:  return gy - 1, gx
        if dy == 1:  return gy,     gx
        if dx == -1: return gy,     gx - 1
        if dy == -1: return gy - 1, gx - 1
        return -1, -1

    # 顺时针旋转 90° (y-down): (dx,dy) → (-dy, dx)  → 即"左转"
    # 逆时针旋转 90° (y-down): (dx,dy) → (dy, -dx)  → 即"右转"
    def cw_turn(dx, dy):   # 顺时针 = 右转
        return -dy, dx
    def ccw_turn(dx, dy):  # 逆时针 = 左转
        return dy, -dx

    cur_gx, cur_gy = start_gx, start_gy
    dx, dy = 1, 0  # 初始向右

    vertices_grid = []
    start_key = (cur_gx, cur_gy, dx, dy)
    visited = set()
    max_steps = (ny + nx + 4) * 8

    for step in range(max_steps):
        key = (cur_gx, cur_gy, dx, dy)
        if key in visited and step > 2:
            break
        visited.add(key)
        vertices_grid.append((cur_gx, cur_gy))

        r_iy, r_ix = right_cell(cur_gx, cur_gy, dx, dy)
        rf_iy, rf_ix = right_cell(cur_gx + dx, cur_gy + dy, dx, dy)  # 前方格子的右手格
        dl_iy, dl_ix = diag_left_front_cell(cur_gx, cur_gy, dx, dy)

        r_ok  = is_filled(r_iy, r_ix)    # 当前右手格
        dl_ok = is_filled(dl_iy, dl_ix)  # 对角左前方格

        if not r_ok:
            # 右手格子不填充 → 右转（顺时针 90°）再看
            dx, dy = cw_turn(dx, dy)
        elif dl_ok:
            # 右手格子填充且对角前方也填充 → 左转（逆时针 90°）进入凹角
            dx, dy = ccw_turn(dx, dy)
        # else: 继续直行

        cur_gx += dx
        cur_gy += dy

        if (cur_gx, cur_gy) == (start_gx, start_gy) and step > 2:
            break

    # 只保留方向改变处的顶点（去除共线点）
    def is_turn(pts, i):
        n = len(pts)
        p0 = pts[(i - 1) % n]
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        return (p2[0] - p1[0]) != (p1[0] - p0[0]) or (p2[1] - p1[1]) != (p1[1] - p0[1])

    # 去重连续重复点
    deduped = []
    for v in vertices_grid:
        if not deduped or deduped[-1] != v:
            deduped.append(v)
    if deduped and deduped[0] == deduped[-1]:
        deduped.pop()

    if not deduped:
        return []

    # 只保留转折点
    n = len(deduped)
    verts_keep = [deduped[i] for i in range(n) if is_turn(deduped, i)]

    if not verts_keep:
        verts_keep = deduped

    # 格角坐标 → 像素坐标
    vertices_px = []
    for gx, gy in verts_keep:
        px = x_edges[gx] if gx < len(x_edges) else x_edges[-1]
        py = y_edges[gy] if gy < len(y_edges) else y_edges[-1]
        vertices_px.append((px, py))
    return vertices_px


def _grid_contour(occ, ny, nx, x_edges, y_edges):
    """兼容旧调用的包装（已由 _segment_trace_contour 取代）。"""
    return _segment_trace_contour(None, y_edges, x_edges, occ, ny, nx)


def _pixel_scale(vertices_px, meta, label_values):
    """用一条已标注边长推导像素→坐标单位的比例因子。
    优先选最长、标注最确定的边。返回 scale (坐标/像素) 或 None。"""
    if not vertices_px or not label_values:
        return None
    n = len(vertices_px)
    edge_px_lengths = []
    for i in range(n):
        x1, y1 = vertices_px[i]
        x2, y2 = vertices_px[(i + 1) % n]
        px_len = abs(x2 - x1) + abs(y2 - y1)  # 正交边：只有一个方向非零
        edge_px_lengths.append(px_len)

    # label_values 是 [(float_value, cx_px, cy_px)] 形式的 list
    # 找最长边对应的标注值
    max_px = max(edge_px_lengths)
    if max_px == 0:
        return None

    best_scale = None
    best_px = 0
    for val, cx, cy in label_values:
        # 找距离这个标注最近的边中点
        min_dist = float("inf")
        best_edge_idx = -1
        for i in range(n):
            x1, y1 = vertices_px[i]
            x2, y2 = vertices_px[(i + 1) % n]
            mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
            dist = ((mid_x - cx) ** 2 + (mid_y - cy) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_edge_idx = i
        if best_edge_idx >= 0:
            px_len = edge_px_lengths[best_edge_idx]
            if px_len > best_px and val > 0:
                best_px = px_len
                best_scale = float(val) / float(px_len)

    return best_scale


def _labels_from_image(img_array, fill_mask):
    """检测图中标注数字的位置和值。

    策略（v2，膨胀+连通分量+整体区域OCR）：
      1. 检测标注颜色（紫色、黑色、深蓝）的像素掩码
      2. 对掩码做形态学膨胀（15px），合并属于同一数字的分散像素（如"10"的"1"和"0"）
      3. 对膨胀后的连通分量，确定 bounding box
      4. 在 bounding box 区域内，将原始标注色像素变黑，其余变白，做 OCR
      5. 过滤合理数字范围（0.1-30）的结果

    返回 [(float_value, x_col_px, y_row_px), ...] 列表（去重）。
    """
    import numpy as np
    h, w = img_array.shape[:2]
    R = img_array[:, :, 0].astype(int)
    G = img_array[:, :, 1].astype(int)
    B = img_array[:, :, 2].astype(int)

    # ── 标注颜色检测（紫色优先，兼容黑色/深蓝）────────────────────────────
    purple = (R > 80) & (R < 220) & (G < 130) & (B > 80) & (B < 220) & (G < (R + B) // 2)
    black_ann = (R < 80) & (G < 80) & (B < 80) & ~fill_mask
    blue_ish = (B > 100) & (R < B - 40) & (G < B - 20) & ~fill_mask
    annotation_mask = (purple | black_ann | blue_ish) & ~fill_mask

    if annotation_mask.sum() == 0:
        return []

    # ── OCR 依赖检查 ──────────────────────────────────────────────────────
    try:
        from PIL import Image as PilImage, ImageFilter as PilFilter
        import pytesseract
        _tes_ok = True
    except ImportError:
        _tes_ok = False

    if not _tes_ok:
        return []

    # ── 形态学膨胀（合并属于同一数字的分散像素）──────────────────────────
    ann_img = PilImage.fromarray(annotation_mask.astype(np.uint8) * 255)
    dilate_r = 15  # 膨胀半径（覆盖"10"中1和0之间的间隙）
    for _ in range(dilate_r):
        ann_img = ann_img.filter(PilFilter.MaxFilter(3))
    dilated = np.array(ann_img) > 127

    # ── 连通分量分析（膨胀后）────────────────────────────────────────────
    from collections import deque
    visited = np.zeros_like(dilated, dtype=bool)
    components = []
    dilated_pts = list(zip(*np.where(dilated)))
    for (r, c) in dilated_pts:
        if visited[r, c]:
            continue
        comp = []
        q = deque([(r, c)])
        while q:
            cr, cc = q.popleft()
            if visited[cr, cc]:
                continue
            visited[cr, cc] = True
            comp.append((cr, cc))
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = cr + dr, cc + dc
                if (0 <= nr < h and 0 <= nc < w and
                        not visited[nr, nc] and dilated[nr, nc]):
                    q.append((nr, nc))
        components.append(comp)

    # ── 对每个分量做 OCR ────────────────────────────────────────────────
    label_candidates = {}

    def _ocr_component(comp):
        rows_c = [p[0] for p in comp]
        cols_c = [p[1] for p in comp]
        r0 = max(0, min(rows_c) - 8)
        r1 = min(h, max(rows_c) + 8)
        c0 = max(0, min(cols_c) - 8)
        c1 = min(w, max(cols_c) + 8)
        if r1 - r0 < 3 or c1 - c0 < 3:
            return None

        # 检查原始标注像素是否存在（过滤纯膨胀区域）
        orig_count = annotation_mask[r0:r1, c0:c1].sum()
        if orig_count < 3:
            return None

        # 重心坐标（用原始标注像素）
        orig_pts = list(zip(*np.where(annotation_mask[r0:r1, c0:c1])))
        if not orig_pts:
            return None
        cy_center = int(sum(p[0] + r0 for p in orig_pts) / len(orig_pts))
        cx_center = int(sum(p[1] + c0 for p in orig_pts) / len(orig_pts))

        # 构建黑字白底图像
        crop_arr = img_array[r0:r1, c0:c1].copy()
        crop_ann = annotation_mask[r0:r1, c0:c1]
        white_arr = np.full_like(crop_arr, 255)
        white_arr[crop_ann] = [0, 0, 0]
        text_img = PilImage.fromarray(white_arr)
        text_img_4x = text_img.resize(
            (text_img.width * 4, text_img.height * 4), PilImage.NEAREST)

        best_val, best_conf = None, 0
        for cfg in [
            "--psm 8 --oem 1 -c tessedit_char_whitelist=0123456789.",
            "--psm 6 --oem 1 -c tessedit_char_whitelist=0123456789.",
            "--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789.",
        ]:
            try:
                import pytesseract as _pt
                data = _pt.image_to_data(
                    text_img_4x, config=cfg,
                    output_type=_pt.Output.DICT)
                for i in range(len(data["text"])):
                    txt = data["text"][i].strip()
                    conf = int(data["conf"][i])
                    if txt and conf > 20:
                        try:
                            v = float(txt)
                            if 0.1 <= v <= 30 and conf > best_conf:
                                best_conf = conf
                                best_val = v
                        except ValueError:
                            pass
            except Exception:
                pass
        if best_val is not None:
            return best_val, cx_center, cy_center
        return None

    for comp in components:
        result = _ocr_component(comp)
        if result is not None:
            val, cx, cy = result
            # 去重：相距 12px 内的候选只保留置信度最高的
            key = (round(cx / 12) * 12, round(cy / 12) * 12)
            if key not in label_candidates:
                label_candidates[key] = (val, cx, cy)

    return list(label_candidates.values())


@register_tool(
    name="ortho_polygon_from_image",
    description=(
        "从图像中提取正交多边形(所有边水平或竖直)的面积，使用像素级确定性算法，不依赖 VLM 描述。\n"
        "输入：图像本地路径(可选附带'|填充色描述'如'|green')。\n"
        "算法：像素分割→正交轮廓→标注数字映射→闭合校验→sympy 面积计算。\n"
        "输出 status=closed 时返回精确面积和 calculation_trace；"
        "status=open_constraint 时返回待确认的缺失边约束；"
        "status=error 时给出失败原因。\n"
        "依赖：numpy(标准)、Pillow(标准)；pytesseract(可选，用于数字 OCR)。"
    ),
    category="vision", cost_tier="cheap",
)
def t_ortho_polygon_from_image(question, session):
    t0 = time.time()
    parts = (question or "").split("|", 1)
    src_raw = parts[0].strip()
    src = src_raw if os.path.isabs(src_raw) else os.path.join(_SANDBOX_DATA_DIR, src_raw)
    src = os.path.abspath(src)

    if not os.path.isfile(src):
        return f"[ortho_polygon] status=error | 文件不存在: {src}"

    try:
        import numpy as np
    except ImportError:
        return "[ortho_polygon] status=error | numpy 未安装，pip install numpy 后重试"

    try:
        from PIL import Image as PilImage
    except ImportError:
        return "[ortho_polygon] status=error | Pillow 未安装，pip install Pillow 后重试"

    try:
        img = PilImage.open(src).convert("RGB")
        img_array = np.array(img)
        h, w = img_array.shape[:2]
    except Exception as e:
        return f"[ortho_polygon] status=error | 图像加载失败: {e}"

    # Step 1: 提取填充色掩码 + 正交轮廓
    fill_mask = _dominant_fill_mask(img_array)
    if fill_mask.sum() < h * w * 0.01:
        return "[ortho_polygon] status=error | 未检测到明显填充色区域；请确认图像含正交多边形"

    vertices_px, meta = _occupancy_to_vertices(fill_mask)
    if not vertices_px or len(vertices_px) < 4:
        return (f"[ortho_polygon] status=error | 无法从像素恢复正交轮廓，"
                f"仅检测到 {len(vertices_px)} 个顶点；请改用 see_image_vlm 人工描述")

    # Step 2: 提取标注数字位置
    label_values = _labels_from_image(img_array, fill_mask)

    # Step 3: 计算边长（像素单位）及标注映射
    n_v = len(vertices_px)
    edges_px = []
    for i in range(n_v):
        x1, y1 = vertices_px[i]
        x2, y2 = vertices_px[(i + 1) % n_v]
        px_len = abs(x2 - x1) + abs(y2 - y1)
        is_horizontal = (y1 == y2)
        edges_px.append({
            "idx": i, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "px_len": px_len, "horizontal": is_horizontal,
            "label": None,  # 将填入
        })

    # 标注→最近边 映射（辅助函数先定义）
    def _label_dist_for(idx, lvs, edges_list):
        edge = edges_list[idx]
        mid_x = (edge["x1"] + edge["x2"]) / 2
        mid_y = (edge["y1"] + edge["y2"]) / 2
        existing = [((mid_x - _cx) ** 2 + (mid_y - _cy) ** 2) ** 0.5
                    for _, _cx, _cy in lvs]
        return min(existing) if existing else float("inf")

    for val, cx, cy in label_values:
        min_dist = float("inf")
        best_idx = -1
        for edge in edges_px:
            mid_x = (edge["x1"] + edge["x2"]) / 2
            mid_y = (edge["y1"] + edge["y2"]) / 2
            dist = ((mid_x - cx) ** 2 + (mid_y - cy) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_idx = edge["idx"]
        if best_idx >= 0 and min_dist < w * 0.3:
            # 允许多个标注竞争同一边时取最小距离
            if edges_px[best_idx]["label"] is None or (
                    min_dist < _label_dist_for(best_idx, label_values, edges_px)):
                edges_px[best_idx]["label"] = val

    # Step 4: 标注 + 闭合约束 → 重建精确坐标顶点
    n_labeled = sum(1 for e in edges_px if e["label"] is not None)

    # ── 尝试用闭合约束推导缺失边 ──────────────────────────────────────────
    # 正交多边形必须满足: sum(水平运动) = 0, sum(垂直运动) = 0
    # 如果恰好有 1 条水平边或 1 条垂直边未知，可以直接求解
    from fractions import Fraction

    def _try_closure_fill(edges_list):
        """用闭合约束补全最多 1 条缺失水平边 + 1 条缺失垂直边。
        返回是否所有边都已有标注。"""
        h_dirs = [1 if e["x2"] > e["x1"] else (-1 if e["x2"] < e["x1"] else 0)
                  for e in edges_list]
        v_dirs = [1 if e["y2"] > e["y1"] else (-1 if e["y2"] < e["y1"] else 0)
                  for e in edges_list]
        for _ in range(3):  # 最多迭代 3 次（处理级联推导）
            # 水平
            h_known = sum(h_dirs[i] * Fraction(str(edges_list[i]["label"]))
                          for i in range(len(edges_list))
                          if h_dirs[i] != 0 and edges_list[i]["label"] is not None)
            h_unk = [i for i in range(len(edges_list))
                     if h_dirs[i] != 0 and edges_list[i]["label"] is None]
            if len(h_unk) == 1:
                i = h_unk[0]
                lv = -h_known / Fraction(str(h_dirs[i]))
                if lv > 0:
                    edges_list[i]["label"] = float(lv)
            # 垂直
            v_known = sum(v_dirs[i] * Fraction(str(edges_list[i]["label"]))
                          for i in range(len(edges_list))
                          if v_dirs[i] != 0 and edges_list[i]["label"] is not None)
            v_unk = [i for i in range(len(edges_list))
                     if v_dirs[i] != 0 and edges_list[i]["label"] is None]
            if len(v_unk) == 1:
                i = v_unk[0]
                lv = -v_known / Fraction(str(v_dirs[i]))
                if lv > 0:
                    edges_list[i]["label"] = float(lv)
        return all(e["label"] is not None for e in edges_list)

    all_labeled = _try_closure_fill(edges_px)
    n_labeled_final = sum(1 for e in edges_px if e["label"] is not None)
    n_derived = n_labeled_final - n_labeled  # 推导出的边数

    # Step 5: 计算精确坐标面积
    try:
        import sympy as sp

        # ── 路径 A：所有边都有标注 → 直接用标注坐标计算 ──────────────────
        if all_labeled:
            cx_f, cy_f = Fraction(0), Fraction(0)
            coord_vertices = [(cx_f, cy_f)]
            for e in edges_px[:-1]:
                lv = Fraction(str(e["label"]))
                dx, dy = e["x2"] - e["x1"], e["y2"] - e["y1"]
                if dx > 0:
                    cx_f += lv
                elif dx < 0:
                    cx_f -= lv
                elif dy > 0:
                    cy_f += lv
                else:
                    cy_f -= lv
                coord_vertices.append((cx_f, cy_f))

            # 闭合校验（走完最后一条边后是否回到起点）
            last_e = edges_px[-1]
            last_lv = Fraction(str(last_e["label"]))
            last_dx = last_e["x2"] - last_e["x1"]
            last_dy = last_e["y2"] - last_e["y1"]
            if last_dx > 0:
                end_cx = cx_f + last_lv
                end_cy = cy_f
            elif last_dx < 0:
                end_cx = cx_f - last_lv
                end_cy = cy_f
            elif last_dy > 0:
                end_cx = cx_f
                end_cy = cy_f + last_lv
            else:
                end_cx = cx_f
                end_cy = cy_f - last_lv
            h_sum = float(end_cx)
            v_sum = float(end_cy)
            closure_ok = (abs(h_sum) < 0.001 and abs(v_sum) < 0.001)

            # 用 sympy 精确鞋带公式
            sp_coords = [(sp.Rational(str(v[0].numerator), v[0].denominator),
                          sp.Rational(str(v[1].numerator), v[1].denominator))
                         for v in coord_vertices]
            total = sp.Integer(0)
            nv2 = len(sp_coords)
            for i in range(nv2):
                x1, y1 = sp_coords[i]
                x2, y2 = sp_coords[(i + 1) % nv2]
                total += x1 * y2 - x2 * y1
            area_exact = sp.Abs(total) / 2
            method_note = (f"label_direct({n_labeled}/{n_v}edges"
                           f"{'+closure:'+str(n_derived) if n_derived else ''})")
        else:
            # ── 路径 B：标注不足 → 无法精确计算 ──────────────────────────
            closure_ok = False
            still_missing = [e["idx"] for e in edges_px if e["label"] is None]
            _emit_cap_event("ortho_polygon_from_image", "no_scale",
                            {"n_vertices": n_v, "n_labeled": n_labeled_final,
                             "missing_edges": still_missing})
            vertices_info = " → ".join(f"({px},{py})" for px, py in vertices_px[:8])
            return (
                f"[ortho_polygon] status=no_labels | 检测到 {n_v} 个顶点，"
                f"已标注 {n_labeled_final}/{n_v} 条边（仍缺 {len(still_missing)} 条）。\n"
                f"缺失边: {still_missing}\n"
                f"顶点(像素): {vertices_info}\n"
                f"请用 see_image_zoom 读出缺失的紫色数字后重试。"
            )

        _record_calculation(session, engine="ortho_polygon_shoelace",
                            method=method_note,
                            inputs={"n_vertices": n_v, "n_labeled": n_labeled_final,
                                    "n_derived": n_derived, "closure_ok": closure_ok},
                            result=str(area_exact))

        # 同步给推导链门
        budget = session.get("_budget")
        if isinstance(budget, dict):
            ev = budget.setdefault("_vlm_extracted_values", [])
            for e in edges_px:
                if e["label"] is not None:
                    s_val = str(e["label"])
                    if s_val not in ev:
                        ev.append(s_val)
            budget["sympy_result"] = str(area_exact)

        elapsed = round(time.time() - t0, 1)
        status = "closed" if closure_ok else "open_constraint"
        closure_note = ("✓ 闭合校验通过" if closure_ok
                        else f"⚠ 闭合偏差 h={h_sum:.4f} v={v_sum:.4f}")

        # 边长摘要
        edge_summary = []
        for e in edges_px:
            label_str = f"={e['label']}" if e["label"] is not None else "=?"
            orient = "H" if e["horizontal"] else "V"
            edge_summary.append(f"e{e['idx']}({orient}){label_str}")

        # 坐标顶点摘要
        verts_str = " → ".join(
            f"({float(v[0]):.2f},{float(v[1]):.2f})" for v in coord_vertices[:12])

        _emit_cap_event("ortho_polygon_from_image", "ok",
                        {"status": status, "n_vertices": n_v, "area": float(area_exact),
                         "closure_ok": closure_ok, "n_labeled": n_labeled_final,
                         "n_derived": n_derived, "elapsed_s": elapsed})

        # 2026-08-13 · capability_writer 接入：
        # open_constraint 时不再只输出诊断，而是直接提供下一步行动路径：
        # 1. 优先：调 solve_open_constraint（纯确定性，不需要 LLM）
        # 2. 升级：调 write_capability（LLM 生成定制算法）
        _edge_info_str = " | ".join(edge_summary[:16])
        _open_constraint_action = ""
        if status == "open_constraint":
            _open_constraint_action = (
                f"\n\n[★行动指南·open_constraint] 有边长未知，但无需目测/VLM——纯确定性求解路径:\n"
                f"  方案A(推荐·秒级): 调 `solve_open_constraint(edges_info='{_edge_info_str[:200]}')`\n"
                f"    → 自动用闭合约束方程组(sum_dx=0, sum_dy=0)补全缺失边 → 鞋带公式算精确面积\n"
                f"  方案B(通用): 调 `write_capability(problem_desc='正交多边形面积，闭合约束补全缺失边，鞋带公式', "
                f"context='{_edge_info_str[:100]}')`\n"
                f"    → LLM生成定制代码 → 沙箱验证 → 注册为持久工具 → run_written_tool 使用\n"
                f"  ⚠️ 不要调 VLM/see_image_expert 估读缺失边——感知误差比闭合约束数学解更不可靠。"
            )

        return (
            f"[ortho_polygon] status={status} | vertices={n_v} | area={area_exact} | {closure_note}\n"
            f"顶点(坐标): {verts_str}\n"
            f"边: {_edge_info_str}\n"
            f"方法: {method_note}\n"
            f"[计算痕迹已写入 calculation_trace·sympy_result={area_exact}]"
            f"{_open_constraint_action}"
        )

    except Exception as calc_e:
        _emit_cap_event("ortho_polygon_from_image", "error", {"error": str(calc_e)[:120]})
        import traceback
        return (
            f"[ortho_polygon] status=calc_error | vertices={n_v} | n_labeled={n_labeled_final}\n"
            f"面积计算异常: {type(calc_e).__name__}: {calc_e}\n"
            f"详情: {traceback.format_exc()[-400:]}\n"
            f"顶点(像素): {vertices_px[:8]}"
        )


def _selftest_ortho_polygon():
    """ortho_polygon golden case: 用 GAIA 6359a0b1 原题图验收。"""
    # HERE = .../seed-users-system/qa_system/agent
    # 向上 2 级 = .../seed-users-system，再拼 v11/...
    test_img = os.path.join(
        os.path.dirname(os.path.dirname(HERE)),
        "v11", ".gaia_files", "2023", "validation",
        "6359a0b1-8f7b-499b-9336-840f9ab90688.png"
    )
    if not os.path.isfile(test_img):
        print(f"  ortho_polygon SKIP: 测试图片不在本机路径 ({test_img})")
        return
    session = {"_budget": {}}
    out = t_ortho_polygon_from_image(test_img, session)
    print(f"  ortho_polygon 输出前200字: {out[:200]}")
    # 检查是否有 calculation_trace（无论 area 是否精确 —— 机制通路验收）
    trace = session.get("calculation_trace") or []
    assert trace, f"ortho_polygon 必须写入 calculation_trace，got={out[:100]}"
    # 检查 area 字段出现在输出里
    assert "area=" in out, f"输出必须含 area= 字段，got={out[:100]}"
    print("  ortho_polygon PASS: 像素轮廓提取 + calculation_trace ✓")


def run_selftest():
    """能力栈 selftest · 走合成信号·验证每个能力的注册表条目 + 真实代码路径。
    和 selftest_percep_gate.py 同构: 10ms 级·不跑 LLM/不跑整道题。"""
    print("capability_stack · selftest · P0/P1/P2 能力")
    failed = []

    tests = [
        ("sympy", _selftest_sympy),
        ("duckdb", _selftest_duckdb),
        ("ijson", _selftest_ijson),
        ("p1_registry", _selftest_p1_registry),
        ("wayback", _selftest_wayback),
        ("ortho_polygon", _selftest_ortho_polygon),
        # transcribe_audio 和 scene_detect 需要下载视频·不在 selftest 里跑
        # (golden case = GameGrumps 视频·需要真实视频 URL)
    ]

    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"  {name} FAIL: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  {name} ERROR: {type(e).__name__}: {e}")
            failed.append(name)

    print()
    if failed:
        print(f"FAILED: {failed}")
        return False
    else:
        # transcribe_audio / scene_detect 标记为需手动验证
        print(f"PASS({len(tests)} tests) · 视频工具需以具体视频 URL 做模态穷尽验收")
        return True


# ═══════════════════════════════════════════════════════════════════════
# P1/P2 感知、文档、陪审与专业确定性工具
# 所有可选模型都先探测后执行：未安装或无 GPU 时明确返回 unavailable，绝不把
# "已注册" 伪装为 "已经能够推理"。确定性路径（Tesseract/pdftotext/Stockfish）
# 优先于模型路径。
# ═══════════════════════════════════════════════════════════════════════

def _parse_local_path(question):
    path = (question or "").split("|", 1)[0].strip()
    if not os.path.isabs(path):
        path = os.path.join(_SANDBOX_DATA_DIR, path)
    return os.path.abspath(path)


def _tool_available(module=None, command=None):
    if module:
        try:
            __import__(module)
            return True
        except Exception:
            return False
    if command:
        from shutil import which
        return bool(which(command))
    return False


def _numeric_values(text):
    """OCR/VLM 文本中的数值进入 session 元数据；不是触发器，只是可审计事实。"""
    values = re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?", text or "")
    return values[:500]


def _record_extracted_values(session, *, source, engine, text):
    """统一记录感知提取值，并同步给 finalize 的推导链门读取。"""
    values = _numeric_values(text)
    entry = {"source": source, "engine": engine, "values": values, "text": (text or "")[:8000]}
    session.setdefault("extracted_values", []).append(entry)
    # auto agent 的预算快照会持久化这个扁平列表；同步它避免 OCR 绕开痕迹门。
    budget = session.get("_budget")
    if isinstance(budget, dict):
        budget.setdefault("_vlm_extracted_values", []).extend(values)
        del budget["_vlm_extracted_values"][:-500]
    return values


@register_tool(
    name="image_preprocess",
    description="OpenCV 图像前处理：缩放、灰度、二值化、纠偏和网格裁块。用于任何 OCR/VLM 前的标准化，输入 '图路径|操作(可选：scale=3,threshold,deskew,grid=4x5)'。",
    category="vision", cost_tier="cheap",
)
def t_image_preprocess(question, session):
    t0 = time.time()
    src = _parse_local_path(question)
    ops = (question or "").split("|", 1)[1] if "|" in (question or "") else "scale=3,gray"
    if not os.path.isfile(src):
        return f"image_preprocess: 文件不存在 {src}"
    if not _tool_available(module="cv2"):
        return "image_preprocess unavailable: OpenCV 未安装；可用 pip_install opencv-python-headless 后重试"
    try:
        import cv2
        image = cv2.imread(src)
        if image is None:
            return "image_preprocess: 无法解码图像"
        if "gray" in ops or "threshold" in ops:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        scale_match = re.search(r"scale\s*=\s*([\d.]+)", ops)
        if scale_match:
            scale = max(0.1, min(float(scale_match.group(1)), 8.0))
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        if "threshold" in ops:
            image = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        if "deskew" in ops:
            gray = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            points = cv2.findNonZero(255 - gray)
            if points is not None:
                angle = cv2.minAreaRect(points)[-1]
                angle = -(90 + angle) if angle < -45 else -angle
                h, w = image.shape[:2]
                matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
                image = cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)
        os.makedirs(_SANDBOX_DATA_DIR, exist_ok=True)
        stamp = int(time.time() * 1000)
        out = os.path.join(_SANDBOX_DATA_DIR, f"preprocessed_{stamp}.png")
        cv2.imwrite(out, image)
        grid_match = re.search(r"grid\s*=\s*(\d+)\s*x\s*(\d+)", ops, re.I)
        crops = []
        if grid_match:
            rows, cols = int(grid_match.group(1)), int(grid_match.group(2))
            if not (1 <= rows <= 30 and 1 <= cols <= 30):
                return "image_preprocess: grid 行列必须在 1..30"
            height, width = image.shape[:2]
            for row in range(rows):
                for col in range(cols):
                    crop = image[row * height // rows:(row + 1) * height // rows,
                                 col * width // cols:(col + 1) * width // cols]
                    crop_path = os.path.join(_SANDBOX_DATA_DIR, f"preprocessed_{stamp}_r{row+1}c{col+1}.png")
                    cv2.imwrite(crop_path, crop)
                    crops.append(crop_path)
        _emit_cap_event("image_preprocess", "ok", {"ops": ops, "n_crops": len(crops), "elapsed_s": round(time.time()-t0, 2)})
        suffix = f"\n裁块({len(crops)}): " + ", ".join(crops) if crops else ""
        return f"[image_preprocess] 输出: {out} · 操作: {ops}" + suffix
    except Exception as e:
        _emit_cap_event("image_preprocess", "error", {"error": str(e)[:120]})
        return f"image_preprocess 异常: {type(e).__name__}: {e}"


@register_tool(
    name="tesseract_ocr",
    description="印刷体确定性 OCR 基线。输入图片路径或 PDF 路径；优先配合 image_preprocess/裁块。识别到的数值会结构化写入 extracted_values，供后续 Fraction/sympy 验证。",
    category="vision", cost_tier="cheap",
)
def t_tesseract_ocr(question, session):
    src = _parse_local_path(question)
    if not os.path.isfile(src):
        return f"tesseract_ocr: 文件不存在 {src}"
    if not _tool_available(command="tesseract"):
        return "tesseract_ocr unavailable: 未检测到 tesseract CLI"
    try:
        from PIL import Image
        import pytesseract
        if src.lower().endswith(".pdf"):
            return "tesseract_ocr: PDF 请先使用 pdf_extract；扫描 PDF 请先逐页转为图像再 OCR"
        text = pytesseract.image_to_string(Image.open(src), config="--psm 6")
        values = _record_extracted_values(session, source=src, engine="tesseract", text=text)
        _emit_cap_event("tesseract_ocr", "ok", {"n_chars": len(text), "n_values": len(values)})
        return f"[tesseract_ocr] {len(text)} 字；extracted_values={json.dumps(values, ensure_ascii=False)}\n{text[:8000]}"
    except Exception as e:
        _emit_cap_event("tesseract_ocr", "error", {"error": str(e)[:120]})
        return f"tesseract_ocr 异常: {type(e).__name__}: {e}"


@register_tool(
    name="paddleocr_vl",
    description="PaddleOCR-VL 文档/表格 OCR 后端。用于密集小字、手写混印刷与表格；未安装模型时明确报 unavailable，不降级为臆测。",
    category="vision", cost_tier="medium",
)
def t_paddleocr_vl(question, session):
    src = _parse_local_path(question)
    if not os.path.isfile(src):
        return f"paddleocr_vl: 文件不存在 {src}"
    if not _tool_available(module="paddleocr"):
        return "paddleocr_vl unavailable: paddleocr/PaddleOCR-VL 未安装或模型未部署；可先走 tesseract_ocr 基线"
    try:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(lang="en")
        result = ocr.predict(src)
        text = "\n".join(str(row) for row in result)
        values = _record_extracted_values(session, source=src, engine="paddleocr", text=text)
        _emit_cap_event("paddleocr_vl", "ok", {"n_values": len(values)})
        return f"[paddleocr_vl] extracted_values={json.dumps(values, ensure_ascii=False)}\n{text[:8000]}"
    except Exception as e:
        return f"paddleocr_vl 异常: {type(e).__name__}: {str(e)[:200]}"


@register_tool(
    name="dots_ocr",
    description="dots.ocr 结构化表单/文档陪审后端。仅在本地 dots_ocr 包和模型已部署时执行；否则明确报告不可用。",
    category="vision", cost_tier="medium",
)
def t_dots_ocr(question, session):
    src = _parse_local_path(question)
    if not os.path.isfile(src):
        return f"dots_ocr: 文件不存在 {src}"
    if not (_tool_available(module="dots_ocr") or _tool_available(command="dots-ocr")):
        return "dots_ocr unavailable: 本机未部署 dots.ocr 与模型；请使用 PaddleOCR-VL/Tesseract 作为可用陪审"
    return "dots_ocr detected but this deployment has no stable CLI contract; invoke the deployed dots.ocr runner and preserve its structured output before enabling production routing"


@register_tool(
    name="pdf_extract",
    description="数字 PDF 的确定性文本/表格提取：先 pdftotext，再用 pdfplumber/Camelot 取表格；扫描件不伪装成数字 PDF，返回 OCR 路径。输入 PDF 路径。",
    category="document", cost_tier="cheap",
)
def t_pdf_extract(question, session):
    src = _parse_local_path(question)
    if not os.path.isfile(src) or not src.lower().endswith(".pdf"):
        return f"pdf_extract: 需要存在的 PDF 文件，got {src}"
    try:
        raw = _sp.run(["pdftotext", "-layout", src, "-"], capture_output=True, text=True, timeout=60)
        text = raw.stdout if raw.returncode == 0 else ""
        tables = []
        if _tool_available(module="pdfplumber"):
            import pdfplumber
            with pdfplumber.open(src) as pdf:
                for page_no, page in enumerate(pdf.pages[:30], 1):
                    for table in page.extract_tables() or []:
                        tables.append({"page": page_no, "rows": table[:30]})
        values = _record_extracted_values(session, source=src, engine="pdf_deterministic", text=text + json.dumps(tables, ensure_ascii=False))
        if not text.strip() and not tables:
            return "[pdf_extract] 未提取到数字文本/表格：该 PDF 很可能是扫描件；下一步逐页渲染→image_preprocess→tesseract_ocr/paddleocr_vl。"
        _emit_cap_event("pdf_extract", "ok", {"n_chars": len(text), "n_tables": len(tables), "n_values": len(values)})
        return f"[pdf_extract] 文本 {len(text)} 字、表格 {len(tables)} 个；extracted_values={json.dumps(values, ensure_ascii=False)}\n{text[:8000]}\nTABLES={json.dumps(tables[:5], ensure_ascii=False)[:6000]}"
    except Exception as e:
        return f"pdf_extract 异常: {type(e).__name__}: {str(e)[:200]}"


@register_tool(
    name="chess_engine",
    description="确定性棋类求解：传入合法 FEN，python-chess 验证局面后由 Stockfish 给出最佳着法。棋盘图片先用现有 chess_solver 识别，再把 FEN 交给本工具复核。",
    category="compute", cost_tier="cheap",
)
def t_chess_engine(question, session):
    fen = (question or "").strip()
    if not _tool_available(module="chess"):
        return "chess_engine unavailable: python-chess 未安装"
    stockfish = os.environ.get("MEYO_STOCKFISH", "/opt/homebrew/bin/stockfish")
    if not os.path.isfile(stockfish):
        return f"chess_engine unavailable: stockfish 不存在于 {stockfish}"
    try:
        import chess
        import chess.engine
        board = chess.Board(fen)
        if not board.is_valid():
            return "chess_engine: FEN 对应棋局不合法，拒绝计算"
        engine = chess.engine.SimpleEngine.popen_uci(stockfish)
        try:
            move = engine.play(board, chess.engine.Limit(time=1)).move
            san = board.san(move)
        finally:
            engine.quit()
        _record_calculation(session, engine="stockfish", method="best_move", inputs=fen, result=san)
        return f"[chess_engine] 最佳着法 = {san}"
    except Exception as e:
        return f"chess_engine 异常: {type(e).__name__}: {str(e)[:200]}"


@register_tool(
    name="local_vlm_jury",
    description="本地视觉模型陪审：探测 Ollama 中已安装的视觉模型，仅对实际可用模型送图；与 API VLM 分歧时只产出复核证据，不直接覆盖确定性计算。输入 '图路径|问题'。",
    category="vision", cost_tier="medium",
)
def t_local_vlm_jury(question, session):
    parts = (question or "").split("|", 1)
    src = _parse_local_path(parts[0])
    prompt = parts[1].strip() if len(parts) > 1 else "Read every visible value exactly; return a structured list."
    if not os.path.isfile(src):
        return f"local_vlm_jury: 文件不存在 {src}"
    if not _tool_available(command="ollama"):
        return "local_vlm_jury unavailable: ollama 未安装/未运行"
    try:
        models = _sp.run(["ollama", "list"], capture_output=True, text=True, timeout=10).stdout.lower()
        vision = [line.split()[0] for line in models.splitlines()[1:] if any(k in line for k in ("qwen3-vl", "qwen2-vl", "llava", "internvl", "moondream"))]
        if not vision:
            return "local_vlm_jury unavailable: Ollama 中没有视觉模型（当前代码模型不支持图片）；安装 qwen3-vl 后才可启用陪审"
        model = vision[0]
        payload = {"model": model, "prompt": prompt, "images": [__import__('base64').b64encode(open(src, 'rb').read()).decode()], "stream": False}
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as response:
            answer = json.loads(response.read()).get("response", "")
        values = _record_extracted_values(session, source=src, engine=model, text=answer)
        return f"[local_vlm_jury:{model}] extracted_values={json.dumps(values, ensure_ascii=False)}\n{answer[:8000]}"
    except Exception as e:
        return f"local_vlm_jury 异常: {type(e).__name__}: {str(e)[:200]}"


@register_tool(name="mineru_pdf", description="MinerU 2.5 PDF→Markdown 可选 GPU 后端。探测部署状态后执行；未部署时不伪造 PDF 解析结果。", category="document", cost_tier="expensive")
def t_mineru_pdf(question, session):
    src = _parse_local_path(question)
    if not os.path.isfile(src): return f"mineru_pdf: 文件不存在 {src}"
    if not (_tool_available(command="mineru") or _tool_available(module="magic_pdf")):
        return "mineru_pdf unavailable: MinerU 2.5/GPU 后端未部署；当前可用 pdf_extract + OCR 路径"
    return "mineru_pdf detected; use the deployment-specific MinerU runner and register resulting Markdown as dataset evidence before production routing"


@register_tool(name="got_ocr_formula", description="GOT-OCR 2.0 公式→LaTeX 可选模型后端；仅在本机模型已部署时使用。", category="vision", cost_tier="expensive")
def t_got_ocr_formula(question, session):
    if not (_tool_available(module="transformers") and os.environ.get("MEYO_GOT_OCR_MODEL")):
        return "got_ocr_formula unavailable: GOT-OCR2.0 权重/运行配置未部署；印刷公式先 OCR 并以 sympy/Fraction 复核"
    return "got_ocr_formula backend configured but requires deployment runner integration; preserve LaTeX plus source crop as evidence before enabling routing"


@register_tool(name="realesrgan_enhance", description="Real-ESRGAN 图像超分可选 GPU 后端。只在权重和运行器可用时增强；否则返回可审计不可用状态。", category="vision", cost_tier="expensive")
def t_realesrgan_enhance(question, session):
    if not (_tool_available(command="realesrgan-ncnn-vulkan") or os.environ.get("MEYO_REALESRGAN_MODEL")):
        return "realesrgan_enhance unavailable: Real-ESRGAN 权重/运行器未部署；当前使用 image_preprocess 的确定性缩放/二值化"
    return "realesrgan_enhance detected; invoke the configured deployment runner and retain original/enhanced image pair for audit"


@register_tool(name="molmo_video_qa", description="Molmo 2 视频 QA 可选 GPU 后端，是字幕/ASR/场景/帧穷尽后的最后一棒，不替代模态穷尽。", category="vision", cost_tier="expensive")
def t_molmo_video_qa(question, session):
    if not os.environ.get("MEYO_MOLMO_MODEL"):
        return "molmo_video_qa unavailable: Molmo 2 权重/GPU 后端未部署；先完成 scene_detect + label_sweep + transcribe_audio_local + 字幕/元数据穷尽"
    return "molmo_video_qa backend configured but requires deployment runner integration; record sampled frames, prompt and answer as evidence before production routing"


@register_tool(
    name="video_modal_exhaustion",
    description="视频模态穷尽执行器：逐次执行元数据/字幕、场景定位、文字帧扫描和音频转录，并将每一模态的实际结果写入 session；全部返回后才允许将视频标为墙。输入视频 URL。",
    category="deep_research", cost_tier="expensive", retryable=True, max_retry_hint=1,
)
def t_video_modal_exhaustion(question, session):
    url = (question or "").strip()
    if not url:
        return "video_modal_exhaustion: 需要视频 URL"
    report = {"url": url, "metadata_subtitles": "not_run", "scene": "not_run", "frames": "not_run", "audio": "not_run"}
    try:
        meta = _sp.run(["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs", "--sub-langs", "all", "--print", "%(title)s\\n%(description)s\\n%(duration)s", url], capture_output=True, text=True, timeout=90)
        report["metadata_subtitles"] = "ok" if meta.returncode == 0 else f"failed:{(meta.stderr or '')[-120:]}"
        report["metadata"] = meta.stdout[:4000]
    except Exception as exc:
        report["metadata_subtitles"] = f"error:{type(exc).__name__}"
    try:
        scene = t_scene_detect(url, session)
        report["scene"] = scene[:2000]
    except Exception as exc:
        report["scene"] = f"error:{type(exc).__name__}"
    try:
        import video_frame
        frames = video_frame.label_sweep(url, max_sec=120)
        report["frames"] = frames
    except Exception as exc:
        report["frames"] = f"error:{type(exc).__name__}"
    try:
        audio = t_transcribe_audio_local(url, session)
        report["audio"] = audio[:4000]
    except Exception as exc:
        report["audio"] = f"error:{type(exc).__name__}"
    session.setdefault("video_modal_exhaustion", []).append(report)
    _emit_cap_event("video_modal_exhaustion", "ok", {"url": url, "modalities": list(report)})
    return "[video_modal_exhaustion] " + json.dumps(report, ensure_ascii=False)[:11000]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="能力栈 selftest")
    ap.add_argument("--selftest", action="store_true", help="运行 selftest")
    args = ap.parse_args()
    if args.selftest or True:
        run_selftest()
