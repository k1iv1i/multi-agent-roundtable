"""实验2 · 闭环:验证"拿到更好的评审报告,方案最后真的被改好了吗"。

评审报告不是终点,"方案变好"才是。所以这个实验测的是最终产物。

设计主线:**同一份方案 + 不同质量的报告 → 比最终的缺陷消除率。**

    卷B(12 个雷,固定不变)
         ├─ C0 单Agent 的报告 ──→ 改写Agent ──→ 修订版 ──→ 数还剩几个雷 ──→ 消除率
         ├─ C1 算力对齐组的报告 → 改写Agent ──→ 修订版 ──→ 数还剩几个雷 ──→ 消除率
         └─ C3 完整系统的报告 ──→ 改写Agent ──→ 修订版 ──→ 数还剩几个雷 ──→ 消除率

- 固定不变的尺子:那 12 个雷的清单,从头到尾一个字不改;
- 唯一的变量:拿到的报告是谁写的;
- 完全对齐的部分:起始方案同一份、改写 Agent 同一个、判定标准同一套;
- **试卷与配置都与主对照实验一致**,所以两个实验的结论可以直接串成因果链。

为什么改写必须交给 AI:我知道埋了哪些雷,让我照着报告改方案,我会不由自主地把
报告没提到的雷也顺手修掉,数据当场污染。改写 Agent 物理上看不到雷清单,
而且各组用的是同一个改写 Agent —— 顺带剔除了"人的水平"这个变量。

时序上的防循环:改写只看报告,雷清单要到最后判定时才拿出来对答案。

用法:
    python exp2_closedloop_fixrate.py                    # 卷B,C0/C1/C3,各 3 次
    python exp2_closedloop_fixrate.py --runs 1
    python exp2_closedloop_fixrate.py --groups C0,C3
"""
import argparse
import statistics
import sys
import time
import tomllib
from pathlib import Path

from openai import OpenAI

import experiment_lib as ex
import roundtable_engine as rt

BASE_DIR = Path(__file__).parent
RECORD_PATH = BASE_DIR / "实验记录_闭环.md"
DUMP_DIR = BASE_DIR / "实验产物"


def load_api_key():
    secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    if not secrets.exists():
        sys.exit(f"找不到 {secrets},请先配置 OPENAI_API_KEY")
    with secrets.open("rb") as fp:
        key = tomllib.load(fp).get("OPENAI_API_KEY")
    if not key:
        sys.exit("secrets.toml 里没有 OPENAI_API_KEY")
    return key


def run_one(client, group, title, plan_text, defects, usage, dump_tag):
    """一次闭环:评审 → (存档报告) → 改写 → 修复判定"""
    # 第 1 步:评审,拿到报告
    result = rt.review(client, title, plan_text, plan_type="产品需求(PRD)", config=group, usage=usage)
    report = result["report"]
    judged_text = rt.to_markdown(title, "产品需求(PRD)", result)

    # 报告里指出了几个雷(用来解释后面的消除率上限:改不掉没被指出来的问题)
    # 判定对象与实验A一致:完整评审产出,而不是单独的主持人报告
    found = ex.judge_defects(client, judged_text, defects, usage=usage)

    # 第 2 步:改写 Agent 按评审产出修订方案(它看不到雷清单)
    # 传的是完整评审产出,与判定对象保持一致 —— B0/B1 没有主持人报告,
    # 若只传报告它将什么都看不到,消除率必然为 0,对照就失真了。
    revised = ex.rewrite_plan(client, plan_text, judged_text, usage=usage)

    # 第 3 步:拿雷清单去数修订版还剩几个雷(此时才拿出标准答案)
    fixed = ex.judge_fixed(client, revised, defects, usage=usage)

    # 存档:报告与修订版方案都落盘,作品集里可核查
    DUMP_DIR.mkdir(exist_ok=True)
    (DUMP_DIR / f"{dump_tag}_评审纪要.md").write_text(
        rt.to_markdown(title, "产品需求(PRD)", result), encoding="utf-8"
    )
    (DUMP_DIR / f"{dump_tag}_修订版方案.md").write_text(
        f"# 修订版方案(由改写 Agent 依据 {group} 的评审报告生成)\n\n"
        f"> 改写 Agent 只看到方案原文与评审报告,看不到埋雷清单。\n\n{revised}\n",
        encoding="utf-8",
    )

    n = len(defects)
    highs = [d for d in defects if d["高危"]]
    fixed_list = [d for d in fixed if d["已修复"]]
    return {
        "报告指出雷数": sum(1 for d in found if d["命中"]),
        "缺陷消除数": len(fixed_list),
        "缺陷消除率": len(fixed_list) / n,
        "残留高危数": sum(1 for d in fixed if d["高危"] and not d["已修复"]),
        "高危总数": len(highs),
        "修订版字数": len(revised),
        "报告判定": found,
        "修复判定": fixed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    # 默认与主对照实验完全对齐:同一份试卷(卷B)、同一套配置(C 系列)。
    # 这样"报告更好"和"方案被改得更好"两个结论建立在同一个被测对象上,可以直接串成因果链。
    parser.add_argument("--paper", default="B")
    parser.add_argument("--groups", default="C0,C1,C3",
                        help="对照组,逗号分隔;默认 C0(单Agent) / C1(算力对齐) / C3(完整系统)")
    args = parser.parse_args()

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    title, plan_text, defects = rt.load_paper(args.paper)
    if not defects:
        sys.exit(f"没有读到雷清单,请检查 试卷/基准_卷{args.paper}.json")

    client = OpenAI(api_key=load_api_key(), timeout=120.0, max_retries=2)
    usage = rt.Usage()
    started = time.strftime("%Y-%m-%d %H:%M")

    print(f"试卷:{title}(埋雷 {len(defects)} 个,高危 {sum(1 for d in defects if d['高危'])} 个)")
    print(f"对照组:{groups} × {args.runs} 次\n")

    records = {}
    for group in groups:
        runs = []
        for i in range(1, args.runs + 1):
            print(f"[{group}] 第 {i}/{args.runs} 次:评审 → 改写 → 修复判定 …", end="", flush=True)
            try:
                runs.append(run_one(client, group, title, plan_text, defects, usage, f"{group}_第{i}次"))
            except rt.ReviewError as e:
                sys.exit(f"\n实验中断:{e}")
            r = runs[-1]
            marks = "".join("●" if d["已修复"] else "○" for d in r["修复判定"])
            print(f" 报告指出 {r['报告指出雷数']}/{len(defects)}"
                  f" → 消除 {r['缺陷消除数']}/{len(defects)}({r['缺陷消除率'] * 100:.0f}%) {marks}"
                  f"  残留高危 {r['残留高危数']}/{r['高危总数']}")
        records[group] = runs

    usage.wall_seconds = usage.api_seconds
    write_record(args, title, plan_text, defects, records, usage, started)

    print("\n===== 实验B 结果 =====")
    for group, runs in records.items():
        rate = statistics.mean(r["缺陷消除率"] for r in runs)
        found = statistics.mean(r["报告指出雷数"] for r in runs)
        left = statistics.mean(r["残留高危数"] for r in runs)
        print(f"{group} {rt.CONFIGS[group]['label']}:报告指出 {found:.1f}/{len(defects)}"
              f" → 缺陷消除率 {rate * 100:.0f}%,残留高危 {left:.1f}/{runs[0]['高危总数']}")
    if len(records) >= 2:
        keys = list(records)
        lo = statistics.mean(r["缺陷消除率"] for r in records[keys[0]])
        hi = statistics.mean(r["缺陷消除率"] for r in records[keys[-1]])
        print(f"{keys[0]} → {keys[-1]}:消除率 {(hi - lo) * 100:+.0f} 个百分点")
    print(f"\n总用量:{usage.calls} 次调用 / {usage.total_tokens:,} tokens / 约 {usage.cost_cny:.3f} 元")
    print(f"实验记录:{RECORD_PATH}")
    print(f"评审纪要与修订版方案存档:{DUMP_DIR}")


def write_record(args, title, plan_text, defects, records, usage, started):
    n = len(defects)
    flow = [f"{title}({n} 个雷,固定不变)"]
    for i, group in enumerate(records):
        branch = "└─" if i == len(records) - 1 else "├─"
        flow.append(f"     {branch} {group} {rt.CONFIGS[group]['label']} 的报告 "
                    f"→ 改写Agent → 修订版 → 数还剩几个雷 → 消除率")

    L = [
        "# 实验2 记录 · 闭环:方案最后真的被改好了吗(脚本自动生成)",
        "",
        f"> 生成时间:{started}  ",
        f"> 复现命令:`python exp2_closedloop_fixrate.py --runs {args.runs} --groups {args.groups}`  ",
        f"> 模型:{rt.MODEL};评审与改写 temperature={rt.ROLE_TEMPERATURE};"
        f"裁判 temperature={rt.JUDGE_TEMPERATURE}",
        "",
        "## 一、实验设计",
        "",
        "评审报告不是终点,「方案变好」才是。所以这个实验测最终产物。",
        "",
        "**主线:同一份方案 + 不同质量的报告 → 比最终的缺陷消除率。**",
        "",
        "```",
        *flow,
        "```",
        "",
        "**试卷与配置都与主对照实验一致**,所以两个实验的结论可以直接串成因果链。",
        "",
        "| 要素 | 内容 |",
        "|---|---|",
        "| 固定不变的尺子 | 那 " + str(n) + " 个雷的清单,从头到尾一个字不改 |",
        "| 唯一的变量 | 拿到的评审报告是谁写的 |",
        "| 完全对齐的部分 | 起始方案同一份、改写 Agent 同一个、判定标准同一套 |",
        "",
        "**两个刻意的设计:**",
        "",
        "1. **改写交给 AI,不由我本人改。** 我知道埋了哪些雷,自己动手会不由自主把报告没提到的雷",
        "   也顺手修掉,数据当场污染。改写 Agent 物理上看不到雷清单;各组共用同一个改写 Agent,",
        "   顺带剔除了「人的水平」这个变量。",
        "2. **时序防循环。** 改写这一步只看报告;雷清单要到最后判定时才拿出来对答案。",
        "",
        "**为什么不用「问题闭合率」:** 照着报告把问题改掉,第二轮当然不再提这些问题,",
        "闭合率会恒等于 100% —— 那测的是「我作业做得认真不认真」,不是系统好不好。",
        "而且第二轮评的是改过的方案,被测对象变了,又会犯回旧实验「换试卷」的错。",
        "",
        "## 二、结果",
        "",
        "| 指导来源 | 报告指出雷数 | 缺陷消除数 | **缺陷消除率** | 残留高危 |",
        "|---|---|---|---|---|",
    ]
    for group, runs in records.items():
        found = statistics.mean(r["报告指出雷数"] for r in runs)
        fixedn = statistics.mean(r["缺陷消除数"] for r in runs)
        rate = statistics.mean(r["缺陷消除率"] for r in runs)
        left = statistics.mean(r["残留高危数"] for r in runs)
        L.append(f"| {group} {rt.CONFIGS[group]['label']} | {found:.1f}/{n} | {fixedn:.1f}/{n} | "
                 f"**{rate * 100:.0f}%** | {left:.1f}/{runs[0]['高危总数']} |")

    if len(records) >= 2:
        keys = list(records)
        lo = statistics.mean(r["缺陷消除率"] for r in records[keys[0]])
        hi = statistics.mean(r["缺陷消除率"] for r in records[keys[-1]])
        L += ["", f"**{keys[0]} → {keys[-1]}:缺陷消除率 {lo * 100:.0f}% → {hi * 100:.0f}%,"
                  f"{(hi - lo) * 100:+.0f} 个百分点。**"]

    L += [
        "",
        "**「残留高危」这一列不能省。** 修掉一半缺陷但高危全留着的报告近乎无用 ——",
        "消除率不看严重度会骗人。两组消除率相同时,残留高危更少的那组才是真的更好。",
        "",
        "## 三、因果链条",
        "",
        "```",
        "报告指出的雷更多(主对照实验的缺陷召回率)",
        "        ↓ 因为找得多",
        "改写后方案的缺陷消除率更高(本实验)",
        "        ↓ 因为改得对",
        "高危缺陷残留更少",
        "```",
        "",
        "消除率天然受召回率约束 —— 报告没指出来的问题,改写 Agent 不可能修掉。",
        "这正是因果链条成立的表现,不是缺陷。",
        "",
        "## 四、逐条判定与举证(可审计)",
        "",
    ]
    for group, runs in records.items():
        L.append(f"### {group} {rt.CONFIGS[group]['label']}")
        for i, r in enumerate(runs, 1):
            L += ["", f"**第 {i} 次** — 报告指出 {r['报告指出雷数']}/{n},消除 {r['缺陷消除数']}/{n}"
                      f"({r['缺陷消除率'] * 100:.0f}%),修订版 {r['修订版字数']} 字", "",
                  "| 雷# | 缺陷 | 高危 | 报告指出 | 修订版已修复 | 依据(修订版原文) | 程序校验 |",
                  "|---|---|---|---|---|---|---|"]
            found_map = {d["雷编号"]: d["命中"] for d in r["报告判定"]}
            for d in r["修复判定"]:
                ev = d["依据"].replace("|", "/").replace("\n", " ")
                L.append(f"| {d['雷编号']} | {d['标题'][:24]} | {'★' if d['高危'] else ''} | "
                         f"{'是' if found_map.get(d['雷编号']) else '否'} | "
                         f"{'是' if d['已修复'] else '否'} | {ev[:64]} | {d['程序校验']} |")
        L.append("")

    L += [
        "## 五、存档",
        "",
        "每一次的评审纪要与改写后方案全文都已落盘在 `实验产物/`,可逐字核查:",
        "",
    ]
    for group, runs in records.items():
        for i in range(1, len(runs) + 1):
            L.append(f"- `实验产物/{group}_第{i}次_评审纪要.md` + `实验产物/{group}_第{i}次_修订版方案.md`")

    L += [
        "",
        "## 六、本次实验总用量",
        "",
        f"- 模型调用:{usage.calls} 次",
        f"- tokens:{usage.total_tokens:,}",
        f"- 估算成本:约 {usage.cost_cny:.3f} 元",
    ]
    RECORD_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
