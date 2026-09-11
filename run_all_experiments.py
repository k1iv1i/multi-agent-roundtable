"""串行跑完两个核心实验(串行是为了避免并行把每分钟 token 配额打满)。

两个实验都跑在同一份试卷(卷B)、用同一套配置(C 系列),所以结论可以直接串成因果链:

    多角色找到的问题更多(实验1) → 方案最后被改得更好(实验2)

跑之前建议先读 `预先登记_主对照实验.md`。
总量约 460 次调用、约 1.8 元、25 分钟。
"""
import subprocess
import sys
from pathlib import Path

D = Path(__file__).parent

JOBS = [
    ("exp1_main_multi_vs_single.py", ["--runs", "3"],
     "实验1 · 主对照:四种配置在卷B 上比覆盖广度与缺陷召回"),
    ("exp2_closedloop_fixrate.py", ["--runs", "3"],
     "实验2 · 闭环:不同报告驱动同一改写 Agent,比方案最终的缺陷消除率"),
]

for script, args, desc in JOBS:
    print(f"\n{'=' * 64}\n>>> {desc}\n>>> {script} {' '.join(args)}\n{'=' * 64}", flush=True)
    r = subprocess.run([sys.executable, script, *args], cwd=D)
    if r.returncode != 0:
        print(f"!!! {script} 失败,退出码 {r.returncode}")
        sys.exit(r.returncode)

print("\n全部实验完成。记录见 实验记录_主对照.md 与 实验记录_闭环.md")
