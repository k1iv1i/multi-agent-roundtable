"""AI 产品评审圆桌 · Streamlit 界面。

用法:streamlit run roundtable_app.py
流程:左侧粘贴/上传方案 → 开始评审 → 四位角色发言(带原文引用)→ 圆桌报告 → 改完再评看分歧收敛 → 导出评审纪要。

界面设计取舍(面试可讲):
1. 侧边栏只放"输入与开关",主区只放"结论",避免评审结果被表单挤走;
2. 每轮评审顶部先给"耗时/调用/成本/稳定性"四个数字 —— AI 产品的成本和延迟是产品决策的一部分,不该藏起来;
3. 观点用卡片 + 严重度色条呈现,而不是一长串项目符号:评审报告是用来"扫读定位"的,不是用来通读的;
4. 修改清单用卡片而非表格:P0/P1/P2 需要一眼可辨,长文本在表格里会被截断。
"""
import html
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from openai import OpenAI

import roundtable_engine as rt

st.set_page_config(page_title="AI 产品评审圆桌", page_icon="🛡️", layout="wide")

SEVERITY_STYLE = {
    "高": ("#EF4444", "#FEF2F2", "#B91C1C"),
    "中": ("#F59E0B", "#FFFBEB", "#B45309"),
    "低": ("#10B981", "#ECFDF5", "#047857"),
}
PRIORITY_STYLE = {
    "P0": ("#EF4444", "#FEF2F2"),
    "P1": ("#F59E0B", "#FFFBEB"),
    "P2": ("#64748B", "#F1F5F9"),
}

CSS = """
<style>
:root { --rt-primary:#0052D9; --rt-border:#E6E8EF; --rt-ink:#1F2329; --rt-sub:#646A73; }
[data-testid="stAppViewContainer"] { background:#F6F7FA; }
[data-testid="stMainBlockContainer"] { padding-top:2.2rem; max-width:1320px; }
[data-testid="stSidebar"] { background:#FFFFFF; border-right:1px solid var(--rt-border); }
[data-testid="stSidebar"] h2 { font-size:1rem; letter-spacing:.02em; }
h1, h2, h3, h4 { color:var(--rt-ink); letter-spacing:.01em; }

.rt-hero { background:linear-gradient(105deg,#0052D9 0%,#3A7BFF 55%,#6E9BFF 100%);
  border-radius:16px; padding:22px 26px; color:#fff; margin-bottom:18px;
  box-shadow:0 8px 24px rgba(0,82,217,.18); }
.rt-hero h1 { color:#fff; margin:0 0 6px 0; font-size:1.62rem; }
.rt-hero p { margin:0; opacity:.92; font-size:.92rem; line-height:1.7; }
.rt-flow { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
.rt-step { background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.28);
  border-radius:999px; padding:4px 13px; font-size:.79rem; }

.rt-stats { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin:6px 0 18px 0; }
.rt-stat { background:#fff; border:1px solid var(--rt-border); border-radius:12px; padding:12px 14px; }
.rt-stat .k { font-size:.74rem; color:var(--rt-sub); margin-bottom:4px; }
.rt-stat .v { font-size:1.24rem; font-weight:650; color:var(--rt-ink); }
.rt-stat .u { font-size:.7rem; color:var(--rt-sub); margin-left:3px; font-weight:400; }

.op-card { background:#fff; border:1px solid var(--rt-border); border-left:4px solid #94A3B8;
  border-radius:12px; padding:13px 16px; margin-bottom:10px; }
.op-head { display:flex; align-items:center; gap:8px; margin-bottom:7px; flex-wrap:wrap; }
.op-tag { font-size:.72rem; font-weight:650; border-radius:6px; padding:2px 8px; }
.op-idx { font-size:.72rem; color:var(--rt-sub); }
.op-text { font-size:.95rem; color:var(--rt-ink); line-height:1.65; }
.op-quote { margin-top:8px; background:#F8FAFC; border-left:3px solid #CBD5E1; border-radius:0 8px 8px 0;
  padding:7px 11px; font-size:.85rem; color:#475569; line-height:1.6; }
.op-advice { margin-top:8px; font-size:.87rem; color:#0F172A; line-height:1.6; }
.op-advice b { color:var(--rt-primary); }

.rt-block { background:#fff; border:1px solid var(--rt-border); border-radius:12px;
  padding:14px 18px; margin-bottom:12px; height:100%; }
.rt-block h4 { margin:0 0 10px 0; font-size:.95rem; display:flex; align-items:center; gap:7px; }
.rt-block ul { margin:0; padding-left:18px; }
.rt-block li { font-size:.88rem; color:#334155; line-height:1.75; margin-bottom:5px; }
.rt-empty { font-size:.85rem; color:#94A3B8; }

.fix-card { background:#fff; border:1px solid var(--rt-border); border-radius:12px;
  padding:13px 16px; margin-bottom:10px; }
.fix-head { display:flex; align-items:center; gap:9px; margin-bottom:6px; }
.fix-pill { font-size:.74rem; font-weight:700; border-radius:6px; padding:2px 9px; }
.fix-title { font-size:.95rem; font-weight:600; color:var(--rt-ink); }
.fix-meta { font-size:.82rem; color:var(--rt-sub); line-height:1.65; }
.fix-meta b { color:#334155; font-weight:600; }

.rt-note { background:#fff; border:1px solid var(--rt-border); border-left:4px solid var(--rt-primary);
  border-radius:12px; padding:14px 18px; margin-bottom:12px; font-size:.89rem;
  color:#334155; line-height:1.75; }
.rt-note b { color:var(--rt-primary); }
.rt-round-title { display:flex; align-items:center; gap:10px; margin:6px 0 12px 0; }
.rt-round-no { background:var(--rt-primary); color:#fff; border-radius:8px; padding:2px 10px;
  font-size:.8rem; font-weight:650; }
.rt-round-name { font-size:1.12rem; font-weight:650; color:var(--rt-ink); }

[data-testid="stTabs"] button p { font-size:.9rem; font-weight:600; }
div[data-testid="stExpander"] { border-radius:12px; border:1px solid var(--rt-border); background:#fff; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------- 基础工具 ----------
def esc(value):
    return html.escape(str(value or ""))


def report_metrics(report):
    """统计一份报告的 共识数/分歧数/P0数(用于展示和收敛对比)"""
    consensus = len(report.get("共识", []))
    conflicts = len(report.get("分歧", []))
    p0 = sum(1 for m in report.get("修改清单", []) if m.get("优先级") == "P0")
    return consensus, conflicts, p0


# ---------- 渲染组件 ----------
def render_stats(usage):
    """一轮评审的运行指标条:耗时 / 调用 / tokens / 成本 / 结构化输出稳定性"""
    cells = [
        ("本轮耗时", f"{usage.get('耗时_秒', 0)}", "秒"),
        ("模型调用", f"{usage.get('调用次数', 0)}", "次"),
        ("消耗 tokens", f"{usage.get('tokens', 0):,}", ""),
        ("估算成本", f"{usage.get('成本_元', 0):.3f}", "元"),
        ("JSON 一次成功率", f"{usage.get('JSON一次成功率', 0) * 100:.0f}", "%"),
    ]
    items = "".join(
        f'<div class="rt-stat"><div class="k">{esc(k)}</div>'
        f'<div class="v">{esc(v)}<span class="u">{esc(u)}</span></div></div>'
        for k, v, u in cells
    )
    st.markdown(f'<div class="rt-stats">{items}</div>', unsafe_allow_html=True)
    speedup = usage.get("并发加速比", 0)
    if speedup and speedup > 1.1:
        st.caption(
            f"4 个角色并发评审:串行等价耗时 {usage.get('串行等价耗时_秒')} 秒 → 实际 "
            f"{usage.get('耗时_秒')} 秒,加速 {speedup:.1f}×(角色之间互不依赖,并发不破坏角色隔离)"
        )


def render_opinions(role, opinions):
    """一个角色的全部观点,卡片化 + 严重度色条"""
    if not opinions:
        st.markdown('<div class="rt-empty">本角色未产出观点。</div>', unsafe_allow_html=True)
        return
    for i, op in enumerate(opinions, 1):
        bar, bg, fg = SEVERITY_STYLE.get(op.get("严重度"), ("#94A3B8", "#F1F5F9", "#475569"))
        kind = op.get("类型", "观点")
        tag = f"{kind} · {op.get('严重度', '-')}"
        parts = [
            f'<div class="op-card" style="border-left-color:{bar}">',
            '<div class="op-head">',
            f'<span class="op-tag" style="background:{bg};color:{fg}">{esc(tag)}</span>',
            f'<span class="op-idx">{role["icon"]} {esc(role["name"])} · 第 {i} 条</span>',
            "</div>",
            f'<div class="op-text">{esc(op.get("观点"))}</div>',
        ]
        quotes = rt.quotes_of(op)
        if quotes:
            for q in quotes:
                parts.append(f'<div class="op-quote">原文引用:“{esc(q)}”</div>')
            if len(quotes) >= 2:
                parts.append(
                    '<div class="op-advice" style="color:#0052D9">⇄ 跨章节:该观点同时指认了多处原文</div>'
                )
        else:
            parts.append('<div class="op-quote" style="color:#94A3B8">未引用原文 —— 该观点可信度需人工复核</div>')
        if op.get("建议"):
            parts.append(f'<div class="op-advice"><b>建议</b> · {esc(op.get("建议"))}</div>')
        parts.append("</div>")
        st.markdown("".join(parts), unsafe_allow_html=True)


def render_block(title, items, icon):
    lis = "".join(f"<li>{esc(x)}</li>" for x in items)
    body = f"<ul>{lis}</ul>" if items else '<div class="rt-empty">本轮无此项。</div>'
    st.markdown(
        f'<div class="rt-block"><h4>{icon} {esc(title)}</h4>{body}</div>',
        unsafe_allow_html=True,
    )


def render_fix_list(items):
    if not items:
        st.markdown('<div class="rt-empty">本轮未产出修改清单。</div>', unsafe_allow_html=True)
        return
    order = {"P0": 0, "P1": 1, "P2": 2}
    for m in sorted(items, key=lambda x: order.get(x.get("优先级"), 9)):
        fg, bg = PRIORITY_STYLE.get(m.get("优先级"), ("#64748B", "#F1F5F9"))
        st.markdown(
            f'<div class="fix-card"><div class="fix-head">'
            f'<span class="fix-pill" style="background:{bg};color:{fg}">{esc(m.get("优先级", "-"))}</span>'
            f'<span class="fix-title">{esc(m.get("事项"))}</span></div>'
            f'<div class="fix-meta"><b>依据</b> {esc(m.get("依据"))}<br>'
            f'<b>建议动作</b> {esc(m.get("建议动作"))}</div></div>',
            unsafe_allow_html=True,
        )


# ---------- 评审执行 ----------
def run_review(title, text, plan_type, parallel):
    """执行一轮评审,把结果存进 session_state。失败只给友好提示,不炸 traceback"""
    if not text.strip():
        st.sidebar.warning("方案正文是空的,先粘贴或上传一份方案")
        return
    with st.status("评审进行中…", expanded=True) as status:
        st.write(f"四位评审正在{'并发' if parallel else '顺序'}独立评审(互看不到彼此)…")

        def on_done(key):
            role = next(r for r in rt.ROLES if r["key"] == key)
            st.write(f"{role['icon']} {role['name']} 评审完成")

        try:
            result = rt.review(
                client, title, text, plan_type=plan_type,
                on_role_done=on_done, parallel=parallel,
            )
        except rt.ReviewError as e:
            status.update(label="评审失败", state="error", expanded=True)
            st.error(f"{e}\n\n排查顺序:① 网络能否访问 api.openai.com ② key 是否有效/有余额 ③ 是否触发限流")
            return
        st.write("📋 主持人汇总中…")
        status.update(label="评审完成!", state="complete", expanded=False)

    st.session_state.rounds.append(
        {"title": title, "text": text, "type": plan_type, "result": result}
    )


def show_round(round_data, round_no):
    """展示一轮完整评审:运行指标 + 角色发言 + 圆桌报告 + 导出"""
    result = round_data["result"]
    report = result["report"]

    st.markdown(
        f'<div class="rt-round-title"><span class="rt-round-no">第 {round_no} 轮</span>'
        f'<span class="rt-round-name">《{esc(round_data["title"])}》</span></div>',
        unsafe_allow_html=True,
    )
    render_stats(result.get("usage", {}))

    # 角色发言:四个 tab,每个角色一个
    st.markdown("#### 🗣️ 四位评审的独立观点")
    tabs = st.tabs([f"{r['icon']} {r['name']}" for r in rt.ROLES])
    for tab, role in zip(tabs, rt.ROLES):
        with tab:
            render_opinions(role, result["roles"].get(role["key"], {}).get("观点", []))

    # 圆桌报告
    st.markdown("#### 📋 圆桌报告(主持人汇总)")
    c, f, p0 = report_metrics(report)
    st.markdown(
        f'<div class="rt-stats" style="grid-template-columns:repeat(3,1fr)">'
        f'<div class="rt-stat"><div class="k">共识</div><div class="v">{c}<span class="u">条</span></div></div>'
        f'<div class="rt-stat"><div class="k">分歧</div><div class="v">{f}<span class="u">条</span></div></div>'
        f'<div class="rt-stat"><div class="k">P0 事项</div><div class="v">{p0}<span class="u">条</span></div></div>'
        f"</div>",
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        render_block("共识(多个角色都提到)", report.get("共识", []), "🤝")
    with col2:
        render_block("分歧(角色之间观点冲突)", report.get("分歧", []), "⚔️")
    render_block("风险清单(按影响排序)", report.get("风险清单", []), "⚠️")

    st.markdown("**🛠️ 修改清单(按影响 × 概率排序)**")
    render_fix_list(report.get("修改清单", []))

    st.download_button(
        "⬇️ 导出评审纪要(Markdown)",
        data=rt.to_markdown(round_data["title"], round_data["type"], result),
        file_name=f"评审纪要_第{round_no}轮_{round_data['title']}.md",
        mime="text/markdown",
        key=f"dl_{round_no}",
    )


def show_convergence(idx):
    """与上一轮对比:分歧/P0 是否收敛"""
    prev = st.session_state.rounds[idx - 2]["result"]["report"]
    cur = st.session_state.rounds[idx - 1]["result"]["report"]
    _, f1, p1 = report_metrics(prev)
    _, f2, p2 = report_metrics(cur)
    if f1 > f2 or p1 > p2:
        st.success(
            f"📈 相比上一轮:分歧 {f1} → {f2}(-{f1 - f2}),P0 事项 {p1} → {p2}(-{p1 - p2})——分歧收敛中"
        )
    else:
        st.info(
            f"相比上一轮:分歧 {f1} → {f2},P0 事项 {p1} → {p2}。分歧未减少,方案仍有需要处理的核心争议点"
        )


def show_trend():
    """多轮趋势 + 评测命中率对比:把"评审到底有没有用"画出来"""
    rounds = st.session_state.rounds
    if len(rounds) < 2:
        return
    df = pd.DataFrame(
        [
            {
                "轮次": f"第{i}轮",
                "分歧数": report_metrics(r["result"]["report"])[1],
                "P0 事项数": report_metrics(r["result"]["report"])[2],
            }
            for i, r in enumerate(rounds, 1)
        ]
    ).set_index("轮次")

    st.markdown("#### 📊 多轮趋势")
    evals = st.session_state.evals
    cols = st.columns(2) if len(evals) >= 2 else [st.container()]
    with cols[0]:
        st.caption("分歧与 P0 事项随轮次变化")
        st.bar_chart(df, height=260)
    if len(evals) >= 2:
        with cols[1]:
            hit = pd.DataFrame(
                [{"轮次": f"第{i}轮", "基准命中率": evals[i]["命中率"] * 100} for i in sorted(evals)]
            ).set_index("轮次")
            st.caption("基准集命中率随轮次变化(LLM-as-judge,裁判 temperature=0)")
            st.bar_chart(hit, height=260)
            first, last = sorted(evals)[0], sorted(evals)[-1]
            delta = (evals[last]["命中率"] - evals[first]["命中率"]) * 100
            st.markdown(
                f"第{first}轮 **{evals[first]['命中率'] * 100:.0f}%** → 第{last}轮 "
                f"**{evals[last]['命中率'] * 100:.0f}%**,{'提升' if delta >= 0 else '下降'} "
                f"**{abs(delta):.0f}** 个百分点"
            )


def show_empty_state():
    st.markdown(
        '<div class="rt-note">左侧粘贴或上传一份产品方案,点击 <b>开始评审</b>。'
        "内置一份<b>埋雷试卷</b>(故意植入 12 个已知缺陷,清单公开),"
        "可以直接体验<b>「评审 → 改方案 → 再评」</b>的完整闭环,"
        "并用「评测模式」量出这一轮到底发现了几个。</div>",
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        render_block(
            "为什么要 4 个角色",
            [
                "直接问大模型“帮我挑毛病”,得到的是泛泛而谈的客套话",
                "把角色具象成有动机的人格:用户只挑体验、技术只算成本、商业只算账、反方只拆假设",
                "4 个视角正交,覆盖产品决策最关键的 4 类风险",
            ],
            "🎭",
        )
    with c2:
        render_block(
            "怎么防“正确的废话”",
            [
                "角色隔离:4 次调用互不传对方观点,防止互相附和",
                "强制原文引用:每条观点必须摘录方案原句,没有引用的会被标出需人工复核",
                "结构化输出:观点/类型/严重度/引用/建议 固定字段,程序直接解析",
            ],
            "🛡️",
        )
    with c3:
        render_block(
            "效果怎么衡量",
            [
                "内置评测模式:6 条公开材料整理的基准问题,用 LLM-as-judge 算命中率",
                "消融实验:去掉反方角色再评一次,对比分歧检出差异",
                "每轮实时显示耗时/调用次数/tokens/成本/JSON 一次成功率",
            ],
            "📐",
        )


# ---------- 启动检查 ----------
def resolve_api_key():
    """按 secrets.toml → 环境变量 的顺序取 key。

    st.secrets 在文件不存在时会直接抛 StreamlitSecretNotFoundError,
    所以必须包一层 —— 否则别人拉下代码第一次运行看到的是一屏 traceback,
    而不是"该怎么配置"。首屏体验也是产品的一部分。
    """
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
    except Exception:
        pass
    return os.getenv("OPENAI_API_KEY")


api_key = resolve_api_key()
if not api_key:
    st.warning("还没有配置 API Key,先完成下面任一步再刷新页面。")
    st.markdown(
        """
**方式一(推荐)**:在本文件同级建 `.streamlit/secrets.toml`,写入一行

```toml
OPENAI_API_KEY = "sk-..."
```

**方式二**:设置环境变量后再启动

```powershell
$env:OPENAI_API_KEY="sk-..."
streamlit run roundtable_app.py
```

单次评审 = 5 次 gpt-4o-mini 调用,成本约 0.02 元。
        """
    )
    st.stop()

client = OpenAI(api_key=api_key, timeout=120.0, max_retries=2)

# ---------- 会话状态 ----------
if "rounds" not in st.session_state:
    st.session_state.rounds = []
if "evals" not in st.session_state:
    st.session_state.evals = {}      # {轮次序号: 评测结果}
if "plan_title" not in st.session_state:
    # 不硬编码试卷编号:试卷增删时首屏不该跟着崩
    st.session_state.plan_title, st.session_state.plan_text, _ = rt.load_paper(
        next(iter(rt.PAPERS))
    )

# ---------- 头部 ----------
st.markdown(
    '<div class="rt-hero"><h1>🛡️ AI 产品评审圆桌</h1>'
    "<p>粘贴一份产品方案,四位角色化 AI 评审(用户 / 技术 / 商业 / 反方)独立评审、强制引用原文,"
    "主持人汇总共识、分歧与按影响×概率排序的 P0/P1/P2 修改清单;内置评测模式量化验证评审效果。</p>"
    '<div class="rt-flow">'
    '<span class="rt-step">① 输入方案</span><span class="rt-step">② 4 角色并发独立评审</span>'
    '<span class="rt-step">③ 主持人汇总</span><span class="rt-step">④ P0/P1/P2 修改清单</span>'
    '<span class="rt-step">⑤ 改完再评看分歧收敛</span><span class="rt-step">⑥ 基准集命中率评测</span>'
    "</div></div>",
    unsafe_allow_html=True,
)

# ---------- 侧边栏 ----------
with st.sidebar:
    st.header("📝 方案输入")
    plan_type = st.selectbox(
        "方案类型", ["通用方案", "产品需求(PRD)", "活动策划", "技术方案"],
        help="不同类型会给评审追加对应的关注点(如 PRD 追问验收标准、活动策划追问 ROI)",
    )

    paper_key = st.selectbox(
        "载入内置试卷", list(rt.PAPERS.keys()),
        format_func=lambda k: rt.PAPERS[k]["label"],
        help="评测试卷:一份故意埋入已知缺陷的方案,缺陷清单公开可查,用于量化评审效果",
    )
    if st.button("📄 载入到正文", width="stretch"):
        st.session_state.plan_title, st.session_state.plan_text, _ = rt.load_paper(paper_key)
        st.rerun()

    uploaded = st.file_uploader("或上传方案文件(.md / .txt)", type=["md", "txt"])
    if uploaded is not None and st.session_state.get("uploaded_name") != uploaded.name:
        st.session_state.plan_text = uploaded.getvalue().decode("utf-8", errors="ignore")
        st.session_state.plan_title = Path(uploaded.name).stem
        st.session_state.uploaded_name = uploaded.name
        st.rerun()

    st.text_input("方案标题", key="plan_title")
    st.text_area("方案正文", key="plan_text", height=240)
    st.caption(f"当前 {len(st.session_state.plan_text)} 字")

    parallel = st.toggle(
        "角色并发评审", value=True,
        help="4 个角色互不依赖,并发只影响耗时、不影响角色隔离;关掉可现场对比串行耗时",
    )

    if st.button("🚀 开始评审", type="primary", width="stretch"):
        run_review(st.session_state.plan_title, st.session_state.plan_text, plan_type, parallel)

    if st.session_state.rounds:
        if st.button("🔁 改完再评(用当前正文再来一轮)", width="stretch"):
            run_review(st.session_state.plan_title, st.session_state.plan_text, plan_type, parallel)

    st.caption("一次评审 = 5 次模型调用(4 角色 + 主持人),成本见每轮顶部指标条")

    if st.session_state.rounds:
        st.divider()
        with st.expander("🧪 评测模式", expanded=False):
            st.caption(
                "用“基准关键问题清单”(每行一条)衡量评审报告的覆盖能力。"
                "裁判固定 temperature=0、且必须从报告里摘出原文依据,依据由程序回原文校验,"
                "同一条依据不许覆盖两条基准 —— 三道机制都是为了防止假阳性。"
            )
            if st.button("📥 载入当前试卷的缺陷清单", width="stretch"):
                _, _, defects = rt.load_paper(paper_key)
                # 内置试卷的缺陷带「命中要点/漏检情形」,判定标准比一行标题严格得多
                # (「漏检情形」专门用来堵套话)。这里把结构化清单存下来,
                # 让界面里的评测与对照实验用的是同一套判据。
                st.session_state.baseline_struct = rt.defects_as_baseline(defects)
                st.session_state.baseline_input = "\n".join(d["标题"] for d in defects)
                st.rerun()
            st.text_area("基准关键问题(每行一条)", key="baseline_input", height=130)

            round_options = list(range(1, len(st.session_state.rounds) + 1))
            target = st.selectbox(
                "评测哪一轮", round_options, index=len(round_options) - 1,
                format_func=lambda i: f"第 {i} 轮:{st.session_state.rounds[i - 1]['title']}",
            )
            if st.button("🧮 计算命中率", width="stretch"):
                lines = [b.strip() for b in st.session_state.get("baseline_input", "").split("\n") if b.strip()]
                struct = st.session_state.get("baseline_struct")
                # 文本框没被改动过时,用结构化清单(判定更严);一旦手改过就按文本走。
                if struct and [d["标题"] for d in struct] == lines:
                    items = struct
                else:
                    items = lines
                if not items:
                    st.warning("请先粘贴基准问题,或点上面的“载入当前试卷的缺陷清单”")
                else:
                    try:
                        st.session_state.evals[target] = rt.eval_hit_rate(
                            client, st.session_state.rounds[target - 1]["result"]["report"], items
                        )
                    except rt.ReviewError as e:
                        st.error(str(e))

            for idx in sorted(st.session_state.evals):
                er = st.session_state.evals[idx]
                st.metric(f"第 {idx} 轮 · 关键问题命中率", f"{er['命中率'] * 100:.0f}%")
                st.markdown(f"✅ 命中 {len(er['命中'])} 条 / ❌ 漏检 {len(er['漏检'])} 条")
                with st.expander(f"第 {idx} 轮逐条判定与依据"):
                    for d in er["明细"]:
                        mark = "✅" if d["是否覆盖"] else "❌"
                        st.markdown(f"{mark} {d['基准问题']}")
                        if d["是否覆盖"]:
                            st.caption(f"依据:“{d['报告依据']}”")
                        elif d.get("程序校验"):
                            st.caption(f"程序校验:{d['程序校验']}")

            st.divider()
            st.caption("消融实验:去掉反方角色再评一次,对比分歧数量,验证多角色设计是否有效")
            if st.button("⚖️ 消融实验(去反方角色再评)", width="stretch"):
                with st.status("消融评审中(3 角色 + 主持人)…", expanded=True):
                    try:
                        st.session_state.ablation = rt.review(
                            client, st.session_state.plan_title, st.session_state.plan_text,
                            plan_type=plan_type, parallel=parallel,
                            roles=[r for r in rt.ROLES if r["key"] != "critic"],
                        )
                    except rt.ReviewError as e:
                        st.error(str(e))
            if "ablation" in st.session_state:
                f_full = len(st.session_state.rounds[-1]["result"]["report"].get("分歧", []))
                f_ab = len(st.session_state.ablation["report"].get("分歧", []))
                st.markdown(f"有反方角色:分歧 **{f_full}** 条 vs 无反方:分歧 **{f_ab}** 条")
                if f_full > f_ab:
                    st.success("反方角色贡献了额外分歧 → 多角色设计有效")
                else:
                    st.info("本次样本无明显差异;面试口径:小样本展示方法,不夸大结论,多次实验取平均")

# ---------- 主区 ----------
if not st.session_state.rounds:
    show_empty_state()
else:
    for i, rd in enumerate(st.session_state.rounds, 1):
        show_round(rd, i)
        if i > 1:
            show_convergence(i)
        st.divider()
    show_trend()
