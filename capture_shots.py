"""作品集截图脚本:自动跑完整流程,产出 4 张作品集截图。

用法(需先启动应用):
    streamlit run roundtable_app.py     # 另一个终端
    python capture_shots.py

改动要点(相比旧版):
- 文件名用英文:中文脚本名在 Windows 上通过命令行传参会被编码破坏;
- 输出目录用相对路径推导,不再硬编码某台电脑的绝对路径;
- 载入内置埋雷试卷(而非旧的样例方案);
- 评测模式改为"载入当前试卷的缺陷清单",并展开逐条判定与依据(可审计是核心卖点);
- 每步都有 DOM 断言,失败时留一张现场图并明确报出是哪一步断言失败。
"""
import re
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJ = Path(__file__).parent
OUT = PROJ.parent / "作品集上传" / "截图"
URL = "http://localhost:8542"

# 解除 Streamlit 的滚动容器限制,让 full_page 截图能拍到全部内容
UNROLL = """
() => {
  const st = document.createElement('style');
  st.textContent = `
    html, body { height: auto !important; overflow: visible !important; }
    [data-testid="stApp"], [data-testid="stAppViewContainer"],
    [data-testid="stMain"], [data-testid="stMainBlockContainer"],
    [data-testid="stVerticalBlock"], [data-testid="stElementContainer"] {
      height: auto !important; max-height: none !important; overflow: visible !important;
    }
    [data-testid="stSidebar"], [data-testid="stSidebarContent"],
    [data-testid="stSidebarUserContent"] {
      position: static !important; height: auto !important; overflow: visible !important;
    }
    header[data-testid="stHeader"] { position: static !important; }
  `;
  document.head.appendChild(st);
}
"""


def settle(page, timeout=20000):
    """等页面真正安定下来再操作。

    Streamlit 靠 websocket 增量重绘,刚加载完或刚点完按钮时,
    页面可能正在重新渲染 —— 此时执行 evaluate 会撞上
    "Execution context was destroyed"。先等网络空闲再等一拍。
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def snap(page, name, expects):
    """截图前先断言页面上确实有该图要展示的内容,避免拍出空壳截图"""
    settle(page)
    # 展开长页面的样式注入偶尔会撞上重绘,重试几次
    for attempt in range(4):
        try:
            page.evaluate(UNROLL)
            break
        except Exception as e:
            if attempt == 3:
                raise
            print(f"  [重试] 页面重绘中,{attempt + 1}/3:{type(e).__name__}", flush=True)
            page.wait_for_timeout(2000)
    page.wait_for_timeout(1500)
    for text, label in expects:
        if page.get_by_text(text, exact=False).count() == 0:
            raise RuntimeError(f"断言失败:{name} 缺少「{label}」")
        print(f"  [断言OK] {label}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / name), full_page=True)
    print(f"[OK] {name}", flush=True)


def open_panel(page, summary_text):
    """确保某个 expander 是展开状态"""
    settle(page, timeout=8000)
    det = page.locator("details").filter(has=page.locator("summary", has_text=summary_text)).first
    if det.count() == 0:
        raise RuntimeError(f"找不到面板:{summary_text}")
    if det.get_attribute("open") is None:
        page.locator("summary", has_text=summary_text).first.click()
        page.wait_for_timeout(900)


def main():
    # 清掉旧截图,避免新旧混在一起
    if OUT.exists():
        for old in OUT.glob("*.png"):
            old.unlink()
            print(f"[清理旧图] {old.name}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        try:
            page.goto(URL, wait_until="load")
            page.wait_for_selector("text=AI 产品评审圆桌", timeout=30000)
            # Streamlit 首屏之后还会经由 websocket 再渲染一轮,等它彻底安定
            settle(page)
            print("[OK] 页面加载", flush=True)

            # 图1:首屏 —— 设计说明 + 内置埋雷试卷
            snap(page, "1_首屏与设计说明.png", [
                ("为什么要 4 个角色", "设计说明卡"),
                ("载入内置试卷", "试卷选择器"),
            ])

            # 跑第一轮评审
            page.get_by_role("button", name=re.compile("开始评审")).click()
            page.wait_for_selector("text=圆桌报告", timeout=300000)

            # 图2:评审结果 —— 运行指标条 + 四角色观点 + 圆桌报告 + 修改清单
            snap(page, "2_评审结果与运行指标.png", [
                ("本轮耗时", "运行指标条"),
                ("估算成本", "成本指标"),
                ("JSON 一次成功率", "稳定性指标"),
                ("四位评审的独立观点", "角色观点区"),
                ("修改清单", "修改清单"),
            ])

            # 图3:评测模式 —— 载入埋雷清单算命中率,并展开逐条判定与依据
            open_panel(page, "评测模式")
            page.get_by_role("button", name=re.compile("载入当前试卷的缺陷清单")).click()
            page.wait_for_timeout(1200)
            open_panel(page, "评测模式")
            page.get_by_role("button", name=re.compile("计算命中率")).click()
            page.wait_for_selector("text=关键问题命中率", timeout=180000)
            open_panel(page, "评测模式")
            try:
                page.locator("summary", has_text="逐条判定与依据").first.click()
                page.wait_for_timeout(900)
            except Exception:
                print("  [warn] 逐条判定面板未能展开", flush=True)
            snap(page, "3_评测模式与逐条举证.png", [
                ("关键问题命中率", "命中率结果"),
                ("逐条判定与依据", "可审计判定"),
            ])

            # 图4:改完再评 —— 分歧收敛 + 多轮趋势
            page.get_by_role("button", name=re.compile("改完再评")).click()
            page.wait_for_selector("text=相比上一轮", timeout=300000)
            snap(page, "4_改完再评与多轮趋势.png", [
                ("相比上一轮", "收敛对比"),
                ("多轮趋势", "趋势图"),
            ])

            # 拍完检查页面没有异常残留
            body = page.inner_text("body")
            for bad in ("Traceback", "StreamlitAPIException", "KeyError"):
                if bad in body:
                    raise RuntimeError(f"页面出现异常:{bad}")
            print("\n全部截图完成,页面无异常", flush=True)

        except Exception:
            # 先把真实原因打出来。之前这一行放在善后代码之后,
            # 结果善后本身抛错时,真正的失败原因被永远掩盖了。
            print("\n=== 截图失败,原始异常 ===", flush=True)
            traceback.print_exc()
            try:
                OUT.mkdir(parents=True, exist_ok=True)
                page.evaluate(UNROLL)
                page.wait_for_timeout(600)
                page.screenshot(path=str(PROJ / "_shot_fail.png"), full_page=True)
                print(f"[失败快照] {PROJ / '_shot_fail.png'}", flush=True)
            except Exception as e2:
                print(f"[warn] 失败快照也没拍成:{type(e2).__name__}", flush=True)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
