"""实验C · 验证多角色的真实价值:覆盖广度。

**跑之前请先读 `预先登记_主对照实验.md`** —— 假设、事前预测、判定规则都写在那里,不因结果修改。

为什么要有实验C:
实验A 用卷A(深度试卷)测出多角色与单 Agent 无显著差异。复盘认定这是**出题错误**:
卷A 的 8 个雷全是跨章节深度推理型,而深度推理是单个读者仔细读就能完成的任务 ——
多派几个人读同一份方案不会让它变容易。多 Agent 的设计目标是**覆盖广度**,不是深度。

所以卷B 专门设计成广度试卷:12 个雷每个都单点可见,但横跨 6 个互不重叠的关注领域。
主指标换成**领域覆盖数**。

四个对照组的输出模板全部统一为跨章节模板,唯一变量是人设配置:
    C0 单Agent → C1 同人设×4(算力对齐) → C2 4角色旧人设 → C3 4角色排他人设

用法:
    python exp1_main_multi_vs_single.py                # 卷B,四组各 3 次
    python exp1_main_multi_vs_single.py --runs 1       # 快速验证链路
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
RECORD_PATH = BASE_DIR / "实验记录_主对照.md"

# 事前预测(与 预先登记_主对照实验.md 第六节一致,跑完后用于对照)
PREDICTION = {
    "C0": "2~3",
    "C1": "3~4",
    "C2": "4~5",
    "C3": "5~6",
}


def load_api_key():
    secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    if not secrets.exists():
        sys.exit(f"找不到 {secrets},请先配置 OPENAI_API_KEY")
    with secrets.open("rb") as fp:
        key = tomllib.load(fp).get("OPENAI_API_KEY")
    if not key:
        sys.exit("secrets.toml 里没有 OPENAI_API_KEY")
    return key


def run_one(client, group, title, plan_text, defects, usage):
    result = rt.review(client, title, plan_text, plan_type="产品需求(PRD)", config=group, usage=usage)
    judged_text = rt.to_markdown(title, "产品需求(PRD)", result)

    defect_detail = ex.judge_defects(client, judged_text, defects, usage=usage)
    flat = ex.flatten_opinions(result)
    classified = ex.classify_opinions(client, plan_text, flat, defects, usage=usage)
    quotes = ex.quote_validity(plan_text, flat)

    # 实验C 不跑 P0 裁判:高危进P0率在实验A 已测得全组接近 0,
    # 而它会让裁判调用数翻倍、更容易触发限流。取舍写在这里,不是遗漏。
    metrics = ex.compute_metrics(
        defect_detail, classified, quotes, None,
        cross_count=ex.count_cross_opinions(plan_text, flat),
    )
    return {"metrics": metrics, "缺陷判定": defect_detail, "观点标注": classified, "引用校验": quotes}


def mean_of(runs, key):
    vals = [r["metrics"].get(key) for r in runs if r["metrics"].get(key) is not None]
    return statistics.mean(vals) if vals else None


def sd_of(runs, key):
    vals = [r["metrics"].get(key) for r in runs if r["metrics"].get(key) is not None]
    return statistics.stdev(vals) if len(vals) > 1 else 0.0


def union_domains(runs):
    """3 次的并集:累计能覆盖几个领域(单次覆盖有随机性,并集反映能力上限)"""
    covered = set()
    for r in runs:
        for dom, ok in r["metrics"]["领域明细"].items():
            if ok:
                covered.add(dom)
    return covered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--paper", default="B")
    parser.add_argument("--groups", default="C0,C1,C2,C3")
    args = parser.parse_args()

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    title, plan_text, defects = rt.load_paper(args.paper)
    if not defects:
        sys.exit(f"没有读到雷清单,请检查 试卷/基准_卷{args.paper}.json")

    domains = list(dict.fromkeys(d["类别"] for d in defects))
    client = OpenAI(api_key=load_api_key(), timeout=120.0, max_retries=2)
    usage = rt.Usage()
    started = time.strftime("%Y-%m-%d %H:%M")

    print(f"试卷:{title}({len(plan_text)} 字)")
    print(f"埋雷 {len(defects)} 个,高危 {sum(1 for d in defects if d['高危'])} 个,"
          f"横跨 {len(domains)} 个领域:{'、'.join(domains)}")
    print(f"对照组:{groups} × {args.runs} 次")
    print(f"事前预测(领域覆盖数):" + "  ".join(f"{g}={PREDICTION.get(g, '?')}" for g in groups) + "\n")

    records = {}
    for group in groups:
        runs = []
        for i in range(1, args.runs + 1):
            print(f"[{group}] 第 {i}/{args.runs} 次 …", end="", flush=True)
            try:
                runs.append(run_one(client, group, title, plan_text, defects, usage))
            except rt.ReviewError as e:
                sys.exit(f"\n实验中断:{e}")
            m = runs[-1]["metrics"]
            hits = "".join("●" if d["命中"] else "○" for d in runs[-1]["缺陷判定"])
            doms = "".join("■" if m["领域明细"][d] else "□" for d in domains)
            print(f" 领域覆盖 {m['领域覆盖数']}/{m['领域总数']} {doms}"
                  f"  召回 {ex.pct(m['缺陷召回率'])} {hits}"
                  f"  空泛 {ex.pct(m['空泛观点率'])}  观点 {m['观点总数']}")
        records[group] = runs

    usage.wall_seconds = usage.api_seconds
    print_summary(records, domains, args)
    write_record(args, title, plan_text, defects, domains, records, usage, started)
    print(f"\n总用量:{usage.calls} 次调用 / {usage.total_tokens:,} tokens / "
          f"约 {usage.cost_cny:.3f} 元 / JSON 一次成功率 {usage.json_first_pass_rate * 100:.0f}%")
    print(f"实验记录:{RECORD_PATH}")


def verdict(records, args):
    """按预先登记的判定规则输出结论(规则在跑之前已写定,此处只是执行)"""
    lines = []
    if "C0" in records and "C3" in records:
        c0, c3 = mean_of(records["C0"], "领域覆盖数"), mean_of(records["C3"], "领域覆盖数")
        sd0, sd3 = sd_of(records["C0"], "领域覆盖数"), sd_of(records["C3"], "领域覆盖数")
        gap_ok = c3 >= c0 + 2
        no_overlap = (c3 - sd3) > (c0 + sd0)
        ok = gap_ok and no_overlap
        lines.append(
            f"**H1(多角色覆盖更广)**:{'成立' if ok else '不成立'} —— "
            f"C0 {c0:.1f}±{sd0:.1f} → C3 {c3:.1f}±{sd3:.1f};"
            f"判据为「差值 ≥ 2 且标准差区间不重叠」,实测差值 {c3 - c0:+.1f}、"
            f"区间{'不重叠' if no_overlap else '重叠'}。"
        )
    if "C2" in records and "C3" in records:
        c2, c3 = mean_of(records["C2"], "领域覆盖数"), mean_of(records["C3"], "领域覆盖数")
        lines.append(
            f"**H2(排他人设更好)**:{'成立' if c3 > c2 else '不成立'} —— "
            f"C2 {c2:.1f} → C3 {c3:.1f}({c3 - c2:+.1f})。"
        )
    return lines


def print_summary(records, domains, args):
    print("\n===== 实验C 结果 =====")
    print(f"{'组':<28} {'领域覆盖':<12} {'3次并集':<10} {'缺陷召回':<10} {'空泛率':<8} 观点数")
    for group, runs in records.items():
        cov = mean_of(runs, "领域覆盖数")
        sd = sd_of(runs, "领域覆盖数")
        uni = union_domains(runs)
        print(f"{group + ' ' + rt.CONFIGS[group]['label']:<28} "
              f"{cov:.1f}±{sd:.1f}/{len(domains)}    {len(uni)}/{len(domains)}       "
              f"{ex.pct(mean_of(runs, '缺陷召回率')):<10} "
              f"{ex.pct(mean_of(runs, '空泛观点率')):<8} {mean_of(runs, '观点总数'):.1f}")
    print("\n--- 对照预先登记的预测 ---")
    for group, runs in records.items():
        print(f"  {group}:预测 {PREDICTION.get(group, '?')} / 实测 {mean_of(runs, '领域覆盖数'):.1f}")
    print("\n--- 判定(按预先登记的规则) ---")
    for line in verdict(records, args):
        print("  " + line.replace("**", ""))


def write_record(args, title, plan_text, defects, domains, records, usage, started):
    L = [
        "# 实验1 记录 · 主对照:多角色 vs 单 Agent(脚本自动生成)",
        "",
        f"> 生成时间:{started}  ",
        f"> 复现命令:`python exp1_main_multi_vs_single.py --runs {args.runs} --paper {args.paper}`  ",
        f"> 模型:{rt.MODEL};评审 temperature={rt.ROLE_TEMPERATURE};裁判 temperature={rt.JUDGE_TEMPERATURE}  ",
        "> **假设、事前预测与判定规则见 `预先登记_主对照实验.md`,在本实验运行之前已写定。**",
        "",
        "## 一、实验设计",
        "",
        "**主线:试卷固定,换考生。** 方案一个字不改,只换挑问题的方式,",
        "因此指标差异只能由系统配置造成。",
        "",
        f"被测试卷:{title}",
        f"- {len(defects)} 个已知缺陷,**每个单点可见**(不需要交叉比对多个章节),",
        f"  但**横跨 {len(domains)} 个互不重叠的关注领域**:{'、'.join(domains)},每领域 2 个;",
        "- 这样设计是为了把「覆盖广度」单独隔离出来测 —— 多 Agent 的设计目标就是覆盖更多关注面;",
        f"- 刻意让领域数({len(domains)})多于角色数(4),避免「一个角色对一个领域」的过度贴合。",
        "",
        "**主指标:领域覆盖数** —— 几个领域里至少有一个缺陷被发现。",
        "",
        "四组的输出模板**完全统一**,唯一变量是人设配置,所以差异不会掺入模板因素。",
        "",
        "| 组 | 配置 | 调用数 |",
        "|---|---|---|",
    ]
    for group in records:
        spec = rt.CONFIGS[group]
        L.append(f"| {group} | {spec['desc']} | {len(spec['reviewers']) + (1 if spec['host'] else 0)} |")

    L += ["", "## 二、结果", "",
          f"| 组 | **领域覆盖数**(满分 {len(domains)}) | {args.runs}次并集 | 缺陷召回 | 高危召回 | 空泛观点率 | 引用有效率 | 观点数 |",
          "|---|---|---|---|---|---|---|---|"]
    for group, runs in records.items():
        cov, sd = mean_of(runs, "领域覆盖数"), sd_of(runs, "领域覆盖数")
        uni = union_domains(runs)
        L.append(
            f"| **{group}** {rt.CONFIGS[group]['label']} | **{cov:.1f} ±{sd:.1f}** | "
            f"{len(uni)}/{len(domains)} | {ex.pct(mean_of(runs, '缺陷召回率'))} | "
            f"{ex.pct(mean_of(runs, '高危召回率'))} | {ex.pct(mean_of(runs, '空泛观点率'))} | "
            f"{ex.pct(mean_of(runs, '引用有效率'))} | {mean_of(runs, '观点总数'):.1f} |"
        )

    L += ["", "### 对照预先登记的事前预测", "", "| 组 | 事前预测 | 实测 | 是否落在预测区间 |", "|---|---|---|---|"]
    for group, runs in records.items():
        pred = PREDICTION.get(group, "?")
        actual = mean_of(runs, "领域覆盖数")
        try:
            lo, hi = (float(x) for x in pred.split("~"))
            inside = "是" if lo <= actual <= hi else "**否**"
        except ValueError:
            inside = "—"
        L.append(f"| {group} | {pred} | {actual:.1f} | {inside} |")

    L += ["", "### 关键对比", ""]
    for a, b, q in rt.CONFIG_CONTRASTS:
        if a not in records or b not in records:
            continue
        ma, mb = mean_of(records[a], "领域覆盖数"), mean_of(records[b], "领域覆盖数")
        L.append(f"- **{a} → {b}**({q}):领域覆盖 {ma:.1f} → {mb:.1f},**{mb - ma:+.1f} 个领域**")

    L += ["", "### 判定(按预先登记的规则执行)", ""]
    L += ["- " + line for line in verdict(records, args)]

    L += ["", "## 三、逐领域覆盖情况", "",
          "■ = 该领域至少有 1 个雷被发现　□ = 该领域完全漏掉", "",
          "| 组 | 次 | " + " | ".join(domains) + " | 覆盖 |",
          "|---" * (len(domains) + 3) + "|"]
    for group, runs in records.items():
        for i, r in enumerate(runs, 1):
            cells = ["■" if r["metrics"]["领域明细"][d] else "□" for d in domains]
            L.append(f"| {group} | {i} | " + " | ".join(cells) + f" | {r['metrics']['领域覆盖数']}/{len(domains)} |")

    L += ["", "## 四、角色 × 领域 归因矩阵", "",
          "检验角色分工是否真的生效:对角线亮 = 每个角色守住了自己的领域。", ""]
    for group in ("C2", "C3"):
        if group not in records:
            continue
        merged = [c for r in records[group] for c in r["观点标注"]]
        cats, matrix = ex.attribution_matrix(merged, defects)
        if not matrix:
            L += [f"**{group}**:未命中任何埋雷,无法归因。", ""]
            continue
        L += [f"**{group} {rt.CONFIGS[group]['label']}**(累计 {args.runs} 次)", "",
              "| 角色 | " + " | ".join(cats) + " |", "|---" * (len(cats) + 1) + "|"]
        for role in rt.ROLES_V2:
            counts = matrix.get(role["name"]) or {}
            L.append("| " + role["name"] + " | " + " | ".join(str(counts.get(c, 0)) for c in cats) + " |")
        L.append("")

    L += ["## 五、逐条判定与举证(可审计)", ""]
    for group, runs in records.items():
        L.append(f"### {group} {rt.CONFIGS[group]['label']}")
        for i, r in enumerate(runs, 1):
            m = r["metrics"]
            L += ["", f"**第 {i} 次** — 领域覆盖 {m['领域覆盖数']}/{len(domains)},"
                      f"召回 {ex.pct(m['缺陷召回率'])},空泛 {ex.pct(m['空泛观点率'])}", "",
                  "| 雷# | 领域 | 缺陷 | 高危 | 判定 | 依据 | 程序校验 |",
                  "|---|---|---|---|---|---|---|"]
            for d in r["缺陷判定"]:
                evd = d["依据"].replace("|", "/").replace("\n", " ")
                L.append(f"| {d['雷编号']} | {d['类别']} | {d['标题'][:22]} | {'★' if d['高危'] else ''} | "
                         f"{'命中' if d['命中'] else '漏检'} | {evd[:56]} | {d['程序校验']} |")
        L.append("")

    L += ["## 六、本次实验总用量", "",
          f"- 模型调用:{usage.calls} 次(含重试 {usage.attempts} 次)",
          f"- tokens:{usage.total_tokens:,}",
          f"- 估算成本:约 {usage.cost_cny:.3f} 元",
          f"- 结构化输出一次成功率:{usage.json_first_pass_rate * 100:.0f}%"]
    RECORD_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
