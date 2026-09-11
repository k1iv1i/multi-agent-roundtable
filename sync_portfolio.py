"""把项目二里的代码、试卷、实验记录同步成「作品集上传」的最终交付结构。

交付结构(编号让文件按阅读顺序排列):
    00_从这里开始.md        入口与一页摘要(手写,本脚本不覆盖)
    01_项目说明.md          主文档(手写,本脚本不覆盖)
    02_界面截图/            4 张截图
    03_实验数据/            两个实验的记录(做可读化重命名)+ 预先登记 + README
    04_核心代码/            代码 + 试卷 + README

红线(每次同步都会检查):
- 绝不把 .streamlit/secrets.toml 拷进作品集(含真实 API key);
- 绝不把 话术背稿*.md / 简历项目描述*.md / 交接文档.md 拷进作品集(自用材料);
- 绝不把 归档_不进作品集/ 拷进作品集。
"""
import shutil
from pathlib import Path

PROJ = Path(__file__).parent
ROOT = PROJ.parent
OUT = ROOT / "作品集上传"

SHOTS = OUT / "02_界面截图"
DATA = OUT / "03_实验数据"
CODE = OUT / "04_核心代码"

# 手写的交付文档与 README,同步时不覆盖
KEEP_ROOT = {"00_从这里开始.md", "01_项目说明.md"}

# 实验记录 → 交付文件名(可读化,让读者不用猜哪份是哪份)
RECORD_MAP = {
    "实验记录_主对照.md": "1_主对照_多角色vs单Agent.md",
    "实验记录_闭环.md": "2_闭环_方案缺陷消除率.md",
    "预先登记_主对照实验.md": "3_预先登记（实验前写定）.md",
}

CODE_FILES = [
    "roundtable_engine.py", "roundtable_app.py", "experiment_lib.py",
    "exp1_main_multi_vs_single.py", "exp2_closedloop_fixrate.py",
    "run_all_experiments.py",
]
# capture_shots.py 不交付:它是我给作品集拍图的工具(还要装 playwright),
# 既不是产品功能也不是实验的一环,放进去只会让读者多猜一个文件。

# 原始产物只留一组最有对比价值的:最弱配置 vs 完整系统的第 1 次。
# 18 份全放会把目录淹掉,而读者真正想确认的是"报告和修订版长什么样、差别有多大"。
ARTIFACT_KEEP = [
    ("C0_第1次_评审纪要.md", "示例1_单Agent的评审报告.md"),
    ("C0_第1次_修订版方案.md", "示例2_按单Agent报告改出的方案.md"),
    ("C3_第1次_评审纪要.md", "示例3_完整系统的评审报告.md"),
    ("C3_第1次_修订版方案.md", "示例4_按完整系统报告改出的方案.md"),
]

REQUIREMENTS = (
    "streamlit>=1.40\n"
    "pandas>=2.0\n"
    "openai>=1.40\n"
)


def clean_stale():
    """清掉旧结构残留与临时产物。不动 `截图/`(截图脚本的输出目录,由 sync_shots 搬走)"""
    for old in ("项目说明.md", "核心代码", "实验记录", "作品集上传",
                "评测数据与出处.md", "03_实验数据/原始产物"):
        p = OUT / old
        if not p.exists():
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        print(f"[清理旧结构] {old}")
    # 旧命名的实验记录交付文件
    if DATA.exists():
        for p in DATA.glob("*.md"):
            if p.name != "README.md" and p.name not in RECORD_MAP.values():
                p.unlink()
                print(f"[清理旧记录] 03_实验数据/{p.name}")
    for pat in ("_*.out", "_*.err", "_shot_fail.png", "__pycache__"):
        for p in list(OUT.rglob(pat)) + list(PROJ.rglob(pat)):
            if not p.exists():
                continue
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
            print(f"[清理临时] {p.name}")


def sync_shots():
    """截图源:capture_shots.py 输出到 作品集上传/截图/,这里搬进 02_界面截图/"""
    SHOTS.mkdir(parents=True, exist_ok=True)
    legacy = OUT / "截图"
    if legacy.exists():
        for p in sorted(legacy.glob("*.png")):
            shutil.copy2(p, SHOTS / p.name)
        shutil.rmtree(legacy)
    n = len(list(SHOTS.glob("*.png")))
    print(f"[截图] 02_界面截图/ 共 {n} 张" + ("" if n else "  ← 需先运行 capture_shots.py"))


def sync_data():
    DATA.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in RECORD_MAP.items():
        src = PROJ / src_name
        if src.exists():
            shutil.copy2(src, DATA / dst_name)
            print(f"[实验数据] {dst_name}")
        else:
            print(f"[警告] 缺少 {src_name}")

    art_src = PROJ / "实验产物"
    if art_src.exists():
        art = DATA / "报告与修订版示例"
        art.mkdir(exist_ok=True)
        for src_name, dst_name in ARTIFACT_KEEP:
            src = art_src / src_name
            if src.exists():
                shutil.copy2(src, art / dst_name)
                print(f"[实验数据] 报告与修订版示例/{dst_name}")
            else:
                print(f"[警告] 缺少实验产物 {src_name}")
        note = art / "README.md"
        note.write_text(
            "# 报告与修订版示例\n\n"
            "闭环实验一共产生 18 份文件(3 种配置 × 3 次 × 报告与修订版)。\n"
            "这里只挑出**最有对比价值的一组**:最弱配置与完整系统的第 1 次。\n\n"
            "| 文件 | 是什么 |\n|---|---|\n"
            "| 示例1 | C0 单 Agent 出的评审报告 |\n"
            "| 示例2 | 改写 Agent 拿着示例1 改出来的方案 |\n"
            "| 示例3 | C3 完整系统出的评审报告 |\n"
            "| 示例4 | 改写 Agent 拿着示例3 改出来的方案 |\n\n"
            "对照着看示例1 与示例3,能直接看出报告质量的差距;\n"
            "对照示例2 与示例4,能看出这个差距如何传导到最终方案。\n\n"
            "完整的 18 份可以用 `python exp2_closedloop_fixrate.py --runs 3` 重新生成。\n",
            encoding="utf-8")
        print("[实验数据] 报告与修订版示例/README.md")


def sync_code():
    CODE.mkdir(parents=True, exist_ok=True)
    # 清掉旧脚本名的残留
    for p in CODE.glob("*.py"):
        if p.name not in CODE_FILES:
            p.unlink()
            print(f"[清理旧脚本] 04_核心代码/{p.name}")

    for name in CODE_FILES:
        src = PROJ / name
        if src.exists():
            shutil.copy2(src, CODE / name)
            print(f"[代码] {name}")
        else:
            print(f"[警告] 缺少 {name}")

    (CODE / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")

    papers = CODE / "试卷"
    if papers.exists():
        shutil.rmtree(papers)
    papers.mkdir()
    for p in sorted((PROJ / "试卷").glob("*")):
        shutil.copy2(p, papers / p.name)
        print(f"[代码] 试卷/{p.name}")


def check_redlines():
    print("\n[红线检查]")
    bad = []
    for p in OUT.rglob("*"):
        if not p.is_file():
            continue
        low = p.name.lower()
        if "secret" in low or p.suffix == ".toml":
            bad.append(f"密钥文件泄露:{p.relative_to(OUT)}")
        # 自用材料:面试话术、简历段落、交接文档,都不该给面试官看到
        if any(k in p.name for k in ("话术", "简历", "交接")):
            bad.append(f"自用材料泄露:{p.relative_to(OUT)}")
        if "归档" in str(p.relative_to(OUT)):
            bad.append(f"归档内容误入交付:{p.relative_to(OUT)}")
        if p.name.startswith("_") or p.name.startswith("zz"):
            bad.append(f"临时文件残留:{p.relative_to(OUT)}")
    print("  未发现违规" if not bad else "\n".join("  !!! " + b for b in bad))
    return bad


def main():
    clean_stale()
    print()
    sync_shots()
    sync_data()
    sync_code()

    missing = [k for k in KEEP_ROOT if not (OUT / k).exists()]
    if missing:
        print(f"\n[警告] 缺少手写交付文档:{missing}")

    bad = check_redlines()

    print("\n[最终交付结构]")
    for p in sorted(OUT.rglob("*")):
        rel = p.relative_to(OUT)
        print("  " + "    " * (len(rel.parts) - 1) + (f"{rel.name}/" if p.is_dir() else rel.name))

    total = sum(1 for p in OUT.rglob("*") if p.is_file())
    print(f"\n共 {total} 个文件。" + ("红线检查通过,可以上传。" if not bad else "存在红线问题,请先处理!"))


if __name__ == "__main__":
    main()
