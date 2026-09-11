"""实验专用工具库(与产品代码分离)。

这里放的是**评测实验**需要的东西:埋雷判定、观点定性、引用有效性校验、改写 Agent、指标计算。
刻意不放进 roundtable_engine.py —— 产品代码和实验代码混在一起,读代码的人分不清
"哪些是产品功能、哪些是为了做实验搭的脚手架"。

三条贯穿全文的反假阳性机制:
1. 裁判判"命中"必须从被判文本里摘出原文依据;
2. 摘出来的依据由**程序**回原文检索,检索不到直接作废(模型说了不算);
3. 同一条依据不许覆盖两个雷,重复的作废(堵"一句话冒领两道题")。
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import roundtable_engine as rt

# ---------- 裁判提示词 ----------

DEFECT_JUDGE_SYSTEM = """你是评测裁判。下面给你**一条**已知缺陷(写明了命中要点和判为漏检的情形),
以及一份 AI 生成的评审产出。请判断:这份评审产出有没有指出这一条缺陷。

判定规则(严格执行):
1. 只要评审产出中存在指向该缺陷的内容即算命中,措辞和术语不要求一致;
2. 必须满足"命中要点";凡属"判为漏检的情形"那种程度,判未命中;
3. 命中必须有据:从评审产出里**原样摘录**一句话作为依据,不许转述、不许改写、不许拼接多句;
   摘不出原文的,判未命中;
4. 你只判断"有没有指出这个缺陷",不评价它写得好不好、建议是否高明。

必须只输出一个 JSON 对象,字段顺序必须如下(先核对要点、再给依据、最后下判定),不要输出其他文字:
{
  "命中要点核对": "逐句说明评审产出是否触及了命中要点",
  "报告依据": "从评审产出中原样摘录的句子;没有就写无",
  "是否命中": true,
  "理由": "一句话"
}"""

CLASSIFY_SYSTEM = """你是评审观点的标注员。下面给你一份产品方案原文、一份已知缺陷清单,以及一组评审观点。
请为每条观点标注三件事。

1. 定位类型,三选一:
   - "指向条目":这条观点针对方案里**已经写了**的某一处内容 → 必须从方案原文中**原样摘录**那句话;
   - "指出缺失":这条观点指出方案**缺少**某项具体内容(例如没有回滚方案、没有量化目标) → 写出缺少的是什么;
   - "空泛":放之四海皆可的泛泛之谈。
2. 判定"空泛"的唯一标准:**把这条意见原封不动搬到另一份完全不同的产品方案上,它是否依然成立?**
   依然成立(例如"建议加强测试""要注意用户体验""需要更多数据支撑")→ 空泛;
   只对这份方案成立 → 不是空泛。
3. 这条观点指向的是缺陷清单里的哪一条?不属于清单里任何一条则写 0。

必须只输出一个 JSON 对象,不要输出其他文字:
{
  "标注": [
    {"观点序号": 1, "定位类型": "指向条目", "方案定位": "从方案原文原样摘录的句子", "对应雷编号": 3}
  ]
}
标注条数与顺序必须与观点列表一一对应。"""

REWRITER_SYSTEM = """你是产品方案修订员。下面给你一份产品方案原文,和一份针对它的评审报告。
请严格按评审报告指出的问题修订这份方案。

规则:
1. **只修报告明确指出的问题**;报告没有提到的地方保持原样,不要自行发挥、不要额外优化;
2. 保持方案原有的结构与条目编号,在对应条目上就地修改或补充;
3. 只输出修订后的方案全文,不要输出任何说明、对照表、diff 或代码围栏。"""

FIX_JUDGE_SYSTEM = """你是评测裁判。下面给你**一条**已知缺陷和一份**修订后**的产品方案。
请判断:这份修订后的方案有没有真正修掉这条缺陷。

判定规则(严格执行):
1. 判"已修复"必须从修订后方案里**原样摘录**一句话作为依据,证明该缺陷已被处理;
   摘不出原文的,判未修复;
2. 仅仅是"提到了这个话题"不算修复,必须真正给出了应对措施;
3. 凡属该缺陷"判为漏检的情形"那种程度的处理,视为未修复。

必须只输出一个 JSON 对象,字段顺序必须如下,不要输出其他文字:
{
  "处理情况核对": "说明修订后的方案对该缺陷做了什么",
  "方案依据": "从修订后方案中原样摘录的句子;没有就写无",
  "是否已修复": true,
  "理由": "一句话"
}"""


# ---------- 通用:带程序校验的逐条判定 ----------

def _render_defects(defects, with_rules=True):
    lines = []
    for d in defects:
        lines.append(f"雷#{d['编号']} · {d['标题']}")
        lines.append(f"  [类别] {d['类别']} / [雷型] {d['雷型']} / [严重度] {'高危' if d['高危'] else '一般'}")
        lines.append(f"  [原文位置] {d['原文位置']}")
        if with_rules:
            lines.append(f"  [命中要点] {d['命中要点']}")
            lines.append(f"  [判为漏检的情形] {d['漏检情形']}")
    return "\n".join(lines)


def _align(rows, n, filler):
    """标注器偶尔多给或少给几条,按观点条数对齐,保证分母恒等于观点总数"""
    rows = list(rows)[:n]
    rows += [dict(filler) for _ in range(n - len(rows))]
    return rows


def _render_one_defect(d):
    """单条缺陷的完整描述(逐雷独立判定时的输入)"""
    return "\n".join(
        [
            f"雷#{d['编号']} · {d['标题']}",
            f"  [类别] {d['类别']} / [雷型] {d['雷型']} / [严重度] {'高危' if d['高危'] else '一般'}",
            f"  [方案中的原文位置] {d['原文位置']}",
            f"  [命中要点] {d['命中要点']}",
            f"  [判为漏检的情形] {d['漏检情形']}",
        ]
    )


def _verify(rows, defects, haystack, hit_field, evidence_field):
    """把裁判的原始判定过一遍程序校验,返回逐条明细。

    校验两件事:依据能否在原文中检索到、依据是否被重复使用。
    任一不通过 → 该条判定降级为未命中,并把原因记进"程序校验"字段(可审计)。

    去重按雷编号顺序做贪心分配:一条依据只能"认领"一个雷,后来的重复者作废。
    """
    used = set()
    detail = []
    for d, row in zip(defects, rows):
        evidence = str(row.get(evidence_field) or "").strip()
        hit = rt._as_bool(row.get(hit_field)) and evidence not in ("", "无", "None")
        note = ""
        key = rt._normalize(evidence)
        if hit and not rt._contains(haystack, evidence):
            hit, note = False, "依据在原文中检索不到 → 作废"
        elif hit and key in used:
            hit, note = False, "依据与其他雷重复使用 → 作废"
        elif hit:
            used.add(key)
        detail.append(
            {
                "雷编号": d["编号"],
                "标题": d["标题"],
                "类别": d["类别"],
                "雷型": d["雷型"],
                "高危": d["高危"],
                "命中": hit,
                "依据": evidence or "无",
                "理由": str(row.get("理由") or ""),
                "程序校验": note,
            }
        )
    return detail


def _judge_each(client, system, payload_builder, defects, haystack, hit_field, evidence_field,
                usage=None, tag="裁判"):
    """逐个雷独立判定(并发)。

    为什么不能一次调用判完 8 个雷:实测发现**批量判定会整批偏移** —— 同一套提示词、
    同样 temperature=0,一次调用里判 8 条时,裁判会整批倾向严格或整批倾向宽松
    (出现过"依据摘得完全正确、却把 8 条全判成漏检"的情况)。这是批量判定的锚定效应。
    改成一雷一次独立调用后,每条判定互不影响,结果稳定且可解释。
    代价是调用数 ×8,但可以并发,而且 gpt-4o-mini 单价极低。
    """
    def judge_one(d):
        return rt._call_json(
            client,
            system,
            payload_builder(d),
            usage=usage,
            temperature=rt.JUDGE_TEMPERATURE,
            tag=f"{tag}#{d['编号']}",
        )

    rows = [None] * len(defects)
    with ThreadPoolExecutor(max_workers=min(8, len(defects))) as pool:
        futures = {pool.submit(judge_one, d): i for i, d in enumerate(defects)}
        for fut in as_completed(futures):
            rows[futures[fut]] = fut.result()
    return _verify(rows, defects, haystack, hit_field, evidence_field)


def judge_defects(client, judged_text, defects, usage=None, tag="埋雷裁判"):
    """逐个雷判定"这份评审产出有没有指出它"。返回逐条明细(含程序校验结果)。

    judged_text 传的是**完整评审产出的 Markdown**(报告 + 各评审原始观点),不是单独的主持人报告。
    原因:B0/B1 没有主持人环节,如果只judge主持人报告,等于拿"汇总稿"跟"原始观点"比,
    各组被judge的东西根本不是同一种东西 —— 这会让"主持人汇总时丢失的细节"被错误地
    算成多角色配置的缺点。产品里用户看到的也是"角色发言 + 圆桌报告"两部分,
    所以以完整产出为判定对象既公平,也符合真实使用场景。
    """
    return _judge_each(
        client,
        DEFECT_JUDGE_SYSTEM,
        lambda d: "已知缺陷:\n" + _render_one_defect(d) + "\n\nAI 评审产出:\n" + judged_text,
        defects,
        judged_text,
        "是否命中",
        "报告依据",
        usage=usage,
        tag=tag,
    )


def judge_fixed(client, revised_text, defects, usage=None, tag="修复裁判"):
    """逐个雷判定修订后的方案是否真正修掉了它(同样逐雷独立判定,防批量偏移)。"""
    detail = _judge_each(
        client,
        FIX_JUDGE_SYSTEM,
        lambda d: "已知缺陷:\n" + _render_one_defect(d) + "\n\n修订后的方案全文:\n" + revised_text,
        defects,
        revised_text,
        "是否已修复",
        "方案依据",
        usage=usage,
        tag=tag,
    )
    for row in detail:  # 换个更贴切的字段名
        row["已修复"] = row.pop("命中")
    return detail


# ---------- 观点定性:空泛观点率 / 额外发现 / 角色归因 ----------

def flatten_opinions(result):
    """把各评审槽位的观点摊平成一个列表,保留它来自哪个角色(算归因矩阵要用)"""
    slots = {s["slot"]: s for s in result.get("slots", [])}
    flat = []
    for slot, payload in result.get("roles", {}).items():
        meta = slots.get(slot, {"key": slot, "name": slot})
        for op in payload.get("观点", []):
            flat.append({"角色key": meta["key"], "角色名": meta["name"], "观点": op})
    return flat


def classify_opinions(client, plan_text, flat, defects, usage=None, tag="观点标注"):
    """给每条观点定性:能否定位到方案原文、指向哪个雷。

    为什么需要这一步:只统计"召回率"的话,一个疯狂输出 50 条废话的系统也能刷高分。
    必须有一个精度侧的指标(空泛观点率),而"能不能指到方案的具体某一处"就是它的判据。
    顺带把"系统发现了我没埋的真问题"(额外发现)从噪音里区分出来,不冤枉它。
    """
    if not flat:
        return []
    listed = "\n".join(f"{i}. {op['观点'].get('观点', '')}" for i, op in enumerate(flat, 1))
    brief = "\n".join(f"雷#{d['编号']} · {d['标题']}" for d in defects)
    data = rt._call_json(
        client,
        CLASSIFY_SYSTEM,
        f"产品方案原文:\n{plan_text}\n\n已知缺陷清单:\n{brief}\n\n评审观点:\n{listed}",
        usage=usage,
        temperature=rt.JUDGE_TEMPERATURE,
        tag=tag,
    )
    rows = data.get("标注")
    if not isinstance(rows, list):
        raise rt.ReviewError(f"[{tag}] 输出缺少『标注』数组")
    rows = _align(rows, len(flat), {"定位类型": "空泛", "方案定位": "无", "对应雷编号": 0})

    out = []
    for op, row in zip(flat, rows):
        kind = str(row.get("定位类型") or "").strip()
        anchor = str(row.get("方案定位") or "").strip()
        note = ""
        if kind == "指向条目":
            # 程序校验:说是指向方案里已有的内容,那这句话必须真的在方案原文里
            specific = rt._contains(plan_text, anchor)
            if not specific:
                kind, note = "空泛", "声称指向原文但检索不到 → 按空泛处理"
        elif kind == "指出缺失":
            # 缺失型缺陷无法在原文中引用(方案里本来就没这段话),只要说清缺的是什么就算具体。
            # 这是修正后的判据:旧版要求"必须能引用原文",会把最有价值的缺失型发现误判成空泛。
            specific = anchor not in ("", "无", "None")
            if not specific:
                kind, note = "空泛", "未说明缺少的具体内容 → 按空泛处理"
        else:
            specific = False
        try:
            defect_no = int(row.get("对应雷编号") or 0)
        except (TypeError, ValueError):
            defect_no = 0
        out.append(
            {
                "角色key": op["角色key"],
                "角色名": op["角色名"],
                "观点": op["观点"].get("观点", ""),
                "原文引用": op["观点"].get("原文引用", "无"),
                "定位类型": kind,
                "方案定位": anchor or "无",
                "具体": specific,
                "对应雷编号": defect_no,
                "程序校验": note,
            }
        )
    return out


def quote_validity(plan_text, flat):
    """引用有效率:观点里的"原文引用"能否在方案原文中检索到。

    **纯程序计算,不调用任何模型** —— 所有指标里唯一完全不依赖 LLM 裁判的硬指标,
    零成本、100% 可复现。它也是"强制引用原文防幻觉"这个设计的直接证据。

    B5 起一条观点可以带多处引用,这里按"引用条目"逐条统计(不是按观点统计),
    这样多处引用不会因为其中一处无效就整条作废,统计口径对所有配置一致。
    """
    total = valid = 0
    bad = []
    for op in flat:
        for quote in rt.quotes_of(op["观点"]):
            total += 1
            if rt._contains(plan_text, quote):
                valid += 1
            else:
                bad.append({"角色": op["角色名"], "无效引用": quote[:60]})
    return {
        "引用条目数": total,
        "有效引用数": valid,
        "引用有效率": valid / total if total else None,
        "无效样例": bad[:5],
    }


def domain_coverage(defect_detail):
    """领域覆盖:6 个关注领域中,至少有 1 个雷被发现的领域数量。

    这是实验C 的**主指标**。理由:多 Agent 的设计目标不是"把某个问题挖得更深"
    (深度推理单个读者仔细读就能完成),而是"覆盖更多关注领域"。
    实验A 用的深度试卷测不出这一点 —— 那是出题错误,不是系统没用。

    返回 (覆盖领域数, 领域总数, {领域: 是否覆盖})
    """
    domains = list(dict.fromkeys(d["类别"] for d in defect_detail))
    covered = {dom: any(d["命中"] for d in defect_detail if d["类别"] == dom) for dom in domains}
    return sum(covered.values()), len(domains), covered


def count_cross_opinions(plan_text, flat):
    """统计"跨章节观点"数:带两处以上**有效**原文引用的观点。

    只数有效引用(能在方案原文里检索到的),否则模型随便编两句就能刷高这个数。
    """
    n = 0
    for op in flat:
        valid = [q for q in rt.quotes_of(op["观点"]) if rt._contains(plan_text, q)]
        if len(valid) >= 2:
            n += 1
    return n


# ---------- 指标汇总 ----------

def compute_metrics(defect_detail, classified, quotes, p0_detail=None, cross_count=0):
    """把逐条判定汇总成一组指标。

    p0_detail: "只拿 P0 子报告"再judge一次的结果,用于算高危进P0率;
    cross_count: 带两处以上有效原文引用的观点数(跨章节观点),由调用方统计后传入。
    """
    total = len(defect_detail)
    hits = [d for d in defect_detail if d["命中"]]
    highs = [d for d in defect_detail if d["高危"]]
    high_hits = [d for d in highs if d["命中"]]

    n_op = len(classified)
    vague = [c for c in classified if not c["具体"]]
    extra = [c for c in classified if c["具体"] and c["对应雷编号"] == 0]
    cov_n, cov_total, cov_map = domain_coverage(defect_detail)

    metrics = {
        # 实验C 的主指标:覆盖了几个关注领域
        "领域覆盖数": cov_n,
        "领域总数": cov_total,
        "领域覆盖率": cov_n / cov_total if cov_total else None,
        "领域明细": cov_map,
        "缺陷召回率": len(hits) / total if total else None,
        "命中雷数": len(hits),
        "埋雷总数": total,
        "高危召回率": len(high_hits) / len(highs) if highs else None,
        "观点总数": n_op,
        "空泛观点率": len(vague) / n_op if n_op else None,
        "额外发现数": len(extra),
        "引用有效率": quotes["引用有效率"],
        "引用条目数": quotes["引用条目数"],
        # 跨章节观点数:带两处以上有效原文引用的观点数。
        # 这是 B5 那次迭代"到底有没有真的起作用"的直接证据 ——
        # 如果 B5 的召回率上去了但跨章节观点数没变,说明提升来自别的地方,不是这次改动。
        "跨章节观点数": cross_count,
    }
    if p0_detail is not None:
        p0_high_hits = [d for d in p0_detail if d["高危"] and d["命中"]]
        metrics["高危进P0率"] = len(p0_high_hits) / len(highs) if highs else None
    else:
        metrics["高危进P0率"] = None
    return metrics


def attribution_matrix(classified, defects):
    """角色 × 雷类别 归因矩阵:哪个角色抓到了哪一类雷。

    对角线亮 = 角色分工真的生效;不亮也是真发现(说明人设不够正交,该回去改 prompt)。
    两种结果都有价值,这才叫实验。
    """
    cat_of = {d["编号"]: d["类别"] for d in defects}
    cats = list(dict.fromkeys(d["类别"] for d in defects))
    matrix = {}
    for c in classified:
        if c["对应雷编号"] == 0 or not c["具体"]:
            continue
        cat = cat_of.get(c["对应雷编号"])
        if cat is None:
            continue
        matrix.setdefault(c["角色名"], {}).setdefault(cat, 0)
        matrix[c["角色名"]][cat] += 1
    return cats, matrix


def rewrite_plan(client, plan_text, review_output, usage=None, tag="改写Agent"):
    """按评审产出修订方案,返回修订后的全文。

    review_output 传的是**完整评审产出的 Markdown**(与判定对象一致),
    而不是单独的主持人报告 —— C0 没有主持人环节,只传报告它将什么都看不到。

    为什么必须由 AI 来改、不能我自己改:我知道埋了哪些雷,让我照着报告改方案,
    我会不由自主把报告没提到的雷也顺手修掉,数据当场污染。
    改写 Agent 物理上看不到雷清单,污染的可能性从根上被切断;
    而且各对照组用的是同一个改写 Agent,顺带剔除了"人的水平"这个变量。
    """
    return rt.call_text(
        client,
        REWRITER_SYSTEM,
        f"产品方案原文:\n{plan_text}\n\n评审产出:\n{review_output}",
        usage=usage,
        temperature=rt.ROLE_TEMPERATURE,
        tag=tag,
    )


def pct(value, digits=0):
    """格式化成百分比;指标不适用时显示 —— (例如 B0 没有主持人就没有 P0)"""
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"
