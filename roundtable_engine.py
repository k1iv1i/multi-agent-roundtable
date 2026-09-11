"""评审引擎:Multi-Agent 产品评审圆桌的核心逻辑。

流程:方案 → 4 个角色各自独立评审(互看不到对方)→ 主持人汇总 → 圆桌报告。

设计要点(面试必讲):
1. 角色隔离:4 次调用互不传对方观点,防止附和、防止 4 个人变成 1 个人;
2. JSON mode:每个 Agent 都输出合法 JSON,程序直接解析;
3. 主持人只看书面观点汇总,不做多轮辩论循环(控制成本与复杂度);
4. 并发评审:4 个角色本来就互不依赖,并发执行把等待时间从"4 次串行"压到"1 次最慢的";
5. 温度分工:角色评审保留多样性(0.7),评测裁判固定 0 —— 评测必须可复现;
6. 用量埋点:每轮统计调用次数、tokens、成本、耗时、JSON 一次成功率,把"成本与稳定性"变成看得见的数字。
"""
import json
import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).parent

MODEL = "gpt-4o-mini"

# 温度分工:角色评审要观点多样,评测裁判要可复现(固定 0)
ROLE_TEMPERATURE = 0.7
JUDGE_TEMPERATURE = 0.0

# gpt-4o-mini 计费口径(USD / 1M tokens),用于成本估算
PRICE_INPUT_USD = 0.15
PRICE_OUTPUT_USD = 0.60
USD_TO_CNY = 7.1

# 四个评审角色的"人设说明书"(Agent 的核心)
#
# ROLES_V1 是最初版本:只写了"你关心什么",没写"你不许说什么"。
# 实验A 的角色归因矩阵暴露了问题:四个角色关注面高度重叠 ——
# 技术角色抓用户类缺陷比抓本职还多,商业类缺陷四个角色一次都没抓到。
# 既然角色不正交,本来就不该期待多角色比单 Agent 覆盖更广。
# 这一版保留下来作为对照组,用于量化"人设排他性"这一项改动的贡献。
ROLES_V1 = [
    {
        "key": "user",
        "name": "目标用户画像",
        "icon": "👤",
        "color": "#3B82F6",
        "domain": "用户体验",
        "persona": "你是一位挑剔的目标用户,只关心:这个方案真的解决我的问题吗?哪里用着别扭?是否符合我的使用习惯?",
    },
    {
        "key": "tech",
        "name": "技术可行性",
        "icon": "⚙️",
        "color": "#8B5CF6",
        "domain": "技术实现",
        "persona": "你是一位资深工程师,只关心:实现成本、技术依赖、边界条件、潜在技术风险。",
    },
    {
        "key": "business",
        "name": "商业化视角",
        "icon": "💰",
        "color": "#F59E0B",
        "domain": "商业模型",
        "persona": "你是一位商业化负责人,只关心:成本结构、定价、市场空间、ROI、与竞品的差异。",
    },
    {
        "key": "critic",
        "name": "反方质疑者",
        "icon": "🔥",
        "color": "#EF4444",
        "domain": "风险合规",
        "persona": "你是一位最尖锐的反对者,只关心:方案的前提假设是否成立?最坏情况是什么?它为什么会失败?",
    },
]

# ROLES_V2:在 V1 基础上给每个角色加上**明确的越界禁令**。
# 设计原则:光说"你关心什么"不够,必须同时说"什么不是你的事" ——
# 否则每个 Agent 都会不自觉地把整份方案从头评一遍,四个角色退化成四个通用评审。
# 排他性还有一个副作用是好的:每个角色的注意力被迫留在自己领域内,会挖得更深。
ROLES_V2 = [
    {
        "key": "user",
        "name": "目标用户画像",
        "icon": "👤",
        "color": "#3B82F6",
        "domain": "用户体验",
        "persona": (
            "你是这个产品的一位挑剔的目标用户。\n"
            "【你的职责】只评论真实使用过程中的体验问题:这个流程我用起来顺不顺?"
            "操作成本是否过高?规则是否符合我的真实作息与使用习惯?异常情况下我会不会被卡住?\n"
            "【明确不属于你的职责】实现难度与技术方案、成本与收入模型、法律合规与数据安全、"
            "运营推广与客服。这些有专门的评审负责,你**不要**发表意见 —— 说了也不算你的功劳,"
            "反而会挤掉你本该发现的体验问题。\n"
            "【你的说话方式】用第一人称讲你自己会遇到的具体场景,不要用产品经理的抽象术语。"
        ),
    },
    {
        "key": "tech",
        "name": "技术可行性",
        "icon": "⚙️",
        "color": "#8B5CF6",
        "domain": "技术实现",
        "persona": (
            "你是一位资深工程师,负责技术实现与数据处理两块。\n"
            "【你的职责】① 技术实现:架构与依赖、性能与容量、边界与异常、"
            "客户端与服务端的信任边界、灰度与回滚;② 数据处理:数据采集范围是否最小必要、"
            "权限与授权方式、留存期限、敏感数据的展示与用户控制权。\n"
            "【明确不属于你的职责】用户的主观体验感受、成本与收入模型、市场竞争、"
            "运营推广与客服承接。这些有专门的评审负责,你**不要**发表意见。\n"
            "【要求】每条意见都要落到方案里具体的技术做法或数据做法上,不要说“建议加强测试”这类空话。"
        ),
    },
    {
        "key": "business",
        "name": "商业化视角",
        "icon": "💰",
        "color": "#F59E0B",
        "domain": "商业模型",
        "persona": (
            "你是一位商业化负责人,负责商业模型与上线后的运营承接两块。\n"
            "【你的职责】① 商业模型:成本结构、补贴与预算上限、收入路径、"
            "对现有业务的替代/蚕食、ROI 与可度量的目标;② 运营承接:上线后谁来接、"
            "客服与纠纷处理的人力、冷启动的供给从哪来、推广节奏与承接能力是否匹配。\n"
            "【明确不属于你的职责】技术实现细节、用户的主观体验感受、法律合规与数据安全。"
            "这些有专门的评审负责,你**不要**发表意见。\n"
            "【要求】尽量把话说到数字上:这笔钱谁出、上限是多少、这个目标怎么验收。"
        ),
    },
    {
        "key": "critic",
        "name": "反方质疑者",
        "icon": "🔥",
        "color": "#EF4444",
        "domain": "风险合规",
        "persona": (
            "你是一位最尖锐的反对者,负责风险与合规。\n"
            "【你的职责】拆解方案赖以成立的前提假设,指出最坏情况:法律与合规风险"
            "(内容责任、资质、监管要求)、人身与财产安全风险、舆情与信任风险、"
            "以及方案会被恶意利用的路径。\n"
            "【明确不属于你的职责】常规的功能优化建议、体验细节、成本测算、技术选型。"
            "别的评审会说这些,你**不要**重复。\n"
            "【你的说话方式】直接指出“这件事会怎么爆”,并且你的建议应当是“如何防止这个最坏情况发生”,"
            "而不是常规的功能改进建议。"
        ),
    },
]

# 产品当前使用的人设版本
ROLES = ROLES_V2

# 方案类型 → 额外提醒评审重点(让"方案类型"这个选项真的影响输出,而不是摆设)
PLAN_TYPE_HINTS = {
    "产品需求(PRD)": "这是一份产品需求文档,请额外关注:需求边界是否清晰、异常与边界流程是否覆盖、验收标准与埋点指标是否可度量。",
    "活动策划": "这是一份活动策划,请额外关注:目标人群与拉新路径、预算与 ROI 测算、活动规则漏洞与舆情风险、活动结束后的承接。",
    "技术方案": "这是一份技术方案,请额外关注:架构依赖与单点、性能与容量假设、灰度与回滚方案、数据一致性与安全合规。",
}

# ---------- 评测试卷 ----------
# 试卷 = 一份故意埋入已知缺陷的方案;标准答案(雷清单)在任何系统运行前就已固定并公开。
# 这样"标准答案从系统输出反推"的循环论证在物理上不可能发生。
PAPER_DIR = BASE_DIR / "试卷"

PAPERS = {
    "B": {
        "label": "卷B · 校园以书换书社区(广度试卷,12 个雷横跨 6 领域)",
        "baseline": "基准_卷B.json",
    },
}

# 兜底文本:试卷文件缺失时(例如只拷走了几个 .py)界面仍能跑起来
SAMPLE_FALLBACK = """1. 背景:平台近期投诉量上升,集中在卖家发货慢、书籍品相与描述不符。
2. 方案:为每个用户计算信用分(0-100,初始 60 分);低于 40 分的卖家商品在搜索中降权;信用分展示在用户主页。
3. 目标:提升平台交易体验,降低投诉率。
4. 数据与实现:基于现有订单、评价、举报数据每日凌晨批量计算,写入用户表。
5. 排期:开发 3 周,测试 1 周,之后全量上线。"""


def _extract_body(path):
    """读方案正文:跳过 markdown 标题与引用说明行,遇到第一条分隔线即停。

    分隔线以下是"出题说明",属于给人看的元信息,绝不能混进被评审的方案正文。
    """
    body = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            break
        if line.startswith("#") or line.lstrip().startswith(">"):
            continue
        body.append(line)
    return "\n".join(body).strip()


def load_paper(paper="B"):
    """载入一份试卷,返回 (标题, 方案正文, 雷清单)。雷清单缺失时返回空列表。"""
    if paper not in PAPERS:
        raise ReviewError(
            f"没有编号为 {paper!r} 的试卷。可用试卷:{list(PAPERS)}"
        )
    meta = PAPERS[paper]
    baseline_path = PAPER_DIR / meta["baseline"]
    if not baseline_path.exists():
        return meta["label"], SAMPLE_FALLBACK, []
    spec = json.loads(baseline_path.read_text(encoding="utf-8"))
    plan_path = PAPER_DIR / spec["方案文件"]
    text = _extract_body(plan_path) if plan_path.exists() else SAMPLE_FALLBACK
    return spec["标题"], text, spec["雷"]


def defects_as_baseline(defects):
    """把雷清单转成界面「评测模式」可用的基准条目(保留命中要点/漏检情形)"""
    return [
        {
            "标题": d["标题"],
            "命中要点": d.get("命中要点", ""),
            "漏检情形": d.get("漏检情形", ""),
        }
        for d in defects
    ]


# 评审 Agent 的输出模板。**四个对照组统一使用它**,所以组间差异只可能来自人设配置。
#
# 两个关键设计:
# ① **原文引用是数组**,允许一条观点同时指认多处原文 ——
#    否则 Agent 想说"第 5.1 条和第 4.3 条互相矛盾"时,连同时引用两处的地方都没有;
# ② **显式列出三类必查项**:前后矛盾 / 目标与手段不匹配 / 数字与假设无依据。
#    这三类都是**通用的产品评审方法**,提示词里从不提及任何具体缺陷,
#    不存在针对某份试卷的答案泄露。
CROSS_OUTPUT_TEMPLATE = """
你必须只输出一个 JSON 对象,不要输出任何其他文字,格式如下:
{
  "观点": [
    {
      "类型": "问题 | 亮点",
      "严重度": "高 | 中 | 低",
      "观点": "你的评审意见(一两句话)",
      "原文引用": ["从方案原文中摘录的原句,可以多条;指出跨章节冲突时必须把冲突的几处都摘出来"],
      "建议": "具体可执行的改进建议"
    }
  ]
}
要求:
- 必须基于方案原文说话,禁止编造方案里没有的内容;
- 除了逐条挑问题,你**必须额外检查以下三类跨章节问题**(它们只有把不同章节对照起来读才会暴露):
  1. **前后矛盾**:方案不同章节的表述是否互相冲突(例如某处说计算是定时批量的、另一处却承诺实时生效;
     或灰度的划分方式与数据的写入方式其实无法共存);
  2. **目标与手段不匹配**:方案宣称的目标,靠它列出的手段真的能达成吗?
     特别注意背景里给出的占比、量级数据 —— 主要矛盾是否恰好没被手段覆盖;
  3. **数字与假设**:方案里每一个数字(阈值、估算量、样本量、比例、周期)有依据吗?
     是否与别处的数字互相矛盾?支撑核心价值的假设是否被真实验证过?
- 报告这三类问题时,"原文引用"必须给出**两处以上**原文,并在"观点"里说明是哪两处在冲突;
- 至少 4 条、最多 8 条观点;
- 保持你的视角,只说你该关心的,别的角色的事不要管。
"""


def quotes_of(opinion):
    """取一条观点的原文引用,统一成列表。

    "原文引用"有两种形态:旧版是单个字符串,现版是数组(允许指认多处)。
    所有消费方(渲染、导出、引用有效率统计)都走这个函数,避免各处重复判类型。
    """
    raw = opinion.get("原文引用")
    if isinstance(raw, list):
        items = raw
    elif raw is None:
        items = []
    else:
        items = [raw]
    return [str(q).strip() for q in items if str(q).strip() not in ("", "无", "None")]

# ---------- 对照实验用的配置 ----------
# 通用评审角色:没有专门视角的"产品评审专家",用于 C0 与 C1 两个对照组。
GENERIC_ROLE = {
    "key": "generic",
    "name": "通用产品评审",
    "icon": "🧑‍💼",
    "color": "#64748B",
    "persona": "你是一位资深产品评审专家,请全面评审这份产品方案,指出其中存在的问题。",
}

# 四个对照组。**四组共用同一个输出模板**(CROSS_OUTPUT_TEMPLATE),
# 唯一的变量是人设配置 —— 组间差异只能来自"用几个人设、人设是否排他"。
#
# 设计要点:C1 是**算力对齐组**。它和 C3 同为 5 次调用、同样有主持人汇总,
# 唯一差别是那 4 次调用用的是不是同一个人设。C1→C3 的差值才是"角色化设计"的净贡献,
# 否则"多角色更好"永远可以被质疑成"你只是多问了几遍"。
CONFIGS = {
    "C0": {
        "label": "单Agent",
        "desc": "1 个通用 Agent,相当于「直接问大模型帮我挑毛病」;基线",
        "reviewers": [GENERIC_ROLE],
        "host": False,
    },
    "C1": {
        "label": "同人设×4+主持人",
        "desc": "同一个通用 Agent 跑 4 遍 + 主持人;**算力对齐组**,与 C2/C3 调用次数完全相同",
        "reviewers": [GENERIC_ROLE] * 4,
        "host": True,
    },
    "C2": {
        "label": "4角色(无越界禁令)+主持人",
        "desc": "四个角色,人设只写了「你关心什么」,没写「什么不是你的事」",
        "reviewers": ROLES_V1,
        "host": True,
    },
    "C3": {
        "label": "4角色(排他人设)+主持人",
        "desc": "四个角色,人设明确写出「什么不是你的事」;**当前产品配置**",
        "reviewers": ROLES_V2,
        "host": True,
    },
}

# 对照组之间要回答的问题(写进实验报告,避免读者自己猜)
CONFIG_CONTRASTS = [
    ("C0", "C1", "把同一人设重复问 4 遍能带来多少提升(纯算力,不涉及角色)"),
    ("C1", "C2", "调用次数对齐的前提下,角色化设计本身的净贡献"),
    ("C2", "C3", "给角色加上越界禁令的贡献"),
    ("C0", "C3", "完整产品 vs 直接问大模型"),
]


def _host_system(roles):
    """主持人说明书(角色名单动态生成,消融实验时角色数会变)"""
    names = "、".join(r["name"] for r in roles)
    return f"""你是评审主持人。你收到 {len(roles)} 位评审({names})的书面意见,他们互相没看过彼此的意见。

请汇总输出一个 JSON 对象,不要输出任何其他文字,格式如下:
{{
  "共识": ["多个角色都提到的问题(标出涉及的角色),没有共识就写空数组"],
  "分歧": ["角色之间观点冲突的地方,没有就写空数组"],
  "风险清单": ["按影响从大到小排序的主要风险"],
  "修改清单": [
    {{"优先级": "P0 | P1 | P2", "事项": "要做什么", "依据": "来自哪位评审的哪条观点", "建议动作": "具体怎么做"}}
  ]
}}
要求:
- 共识/分歧必须真实来自输入的意见,不得编造;
- 修改清单按"影响 × 发生概率"排优先级,P0 表示不做会出大问题;
- 修改清单 3~6 条,聚焦最重要的事项。
"""


class ReviewError(RuntimeError):
    """可预期的失败(网络/鉴权/限流/模型输出始终不合法),由界面做友好提示。"""


class Usage:
    """一轮评审的用量与稳定性埋点。

    面试用途:回答"你的 AI 产品一次要多少钱、多久、稳不稳"时,这里每个数字都能现场指。
    线程安全,因为 4 个角色是并发调用的。
    """

    def __init__(self):
        self.calls = 0            # 成功返回的模型调用次数
        self.attempts = 0         # 实际请求次数(含 JSON 不合法后的重试)
        self.first_pass = 0       # 第一次就输出合法 JSON 的调用数
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.wall_seconds = 0.0   # 整轮墙上时间(并发后≈最慢的那条链路)
        self.api_seconds = 0.0    # 各次调用耗时之和(串行时的等价耗时)
        self._lock = threading.Lock()

    def add_tokens(self, prompt_tokens, completion_tokens):
        with self._lock:
            self.attempts += 1
            self.prompt_tokens += prompt_tokens or 0
            self.completion_tokens += completion_tokens or 0

    def add_call(self, attempts, seconds):
        with self._lock:
            self.calls += 1
            if attempts == 1:
                self.first_pass += 1
            self.api_seconds += seconds

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_cny(self):
        usd = (
            self.prompt_tokens / 1_000_000 * PRICE_INPUT_USD
            + self.completion_tokens / 1_000_000 * PRICE_OUTPUT_USD
        )
        return usd * USD_TO_CNY

    @property
    def json_first_pass_rate(self):
        """结构化输出一次成功率:衡量 JSON mode + 容错重试这条链路的稳定性"""
        return self.first_pass / self.calls if self.calls else 0.0

    @property
    def speedup(self):
        """并发带来的加速比(串行等价耗时 ÷ 实际耗时)"""
        return self.api_seconds / self.wall_seconds if self.wall_seconds else 0.0

    def as_dict(self):
        return {
            "调用次数": self.calls,
            "请求次数": self.attempts,
            "tokens": self.total_tokens,
            "输入tokens": self.prompt_tokens,
            "输出tokens": self.completion_tokens,
            "成本_元": round(self.cost_cny, 4),
            "耗时_秒": round(self.wall_seconds, 1),
            "串行等价耗时_秒": round(self.api_seconds, 1),
            "并发加速比": round(self.speedup, 2),
            "JSON一次成功率": round(self.json_first_pass_rate, 3),
        }


def _parse_json(content):
    """容错解析模型输出:剥掉 markdown 代码围栏和前后杂文,再解析 JSON"""
    text = (content or "").strip()
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


RATE_LIMIT_RETRIES = 6      # 限流最多退避重试几次
RATE_LIMIT_BASE_WAIT = 2.0  # 指数退避的基准秒数


def _is_rate_limit(err):
    """识别限流错误(429 / TPM 打满)。不依赖 openai 的异常类,免得版本差异导致漏判。"""
    name = type(err).__name__
    text = str(err)
    return "RateLimit" in name or "429" in text or "rate_limit" in text


def _request_with_backoff(call, tag):
    """对限流做指数退避重试。

    为什么必须有:实测并发跑实验时把 TPM(每分钟 token 数)打满,拿到 429。
    而 429 的报错里明确写着"Please try again in 173ms" —— 只要等几百毫秒就能恢复,
    但原来的实现直接抛异常,导致跑了几分钟的整个实验从头报废。
    对一个会长时间连续调用模型的产品来说,限流是必然会遇到的常态,不是异常情况。
    """
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return call()
        except Exception as e:
            if not _is_rate_limit(e) or attempt == RATE_LIMIT_RETRIES:
                raise
            wait = RATE_LIMIT_BASE_WAIT * (2 ** attempt) + random.uniform(0, 1)
            print(f"[{tag}] 触发限流,{wait:.1f}s 后重试({attempt + 1}/{RATE_LIMIT_RETRIES})", flush=True)
            time.sleep(wait)


def _call_json(client, system, user, usage=None, temperature=ROLE_TEMPERATURE, retries=2, tag="模型"):
    """统一调用:JSON mode + 解析。解析失败时把错误信息带回去重试,最多 retries 次。

    两类重试是分开的:
    - 限流(429)→ 指数退避后重发同样的请求(_request_with_backoff);
    - JSON 不合法 → 把解析错误带回去让模型重写(下面的 for 循环)。
    失败一律抛 ReviewError(带上是哪个角色出的问题),界面只需要 catch 一种异常。
    """
    last_err = None
    user_msg = user
    attempts = 0
    started = time.perf_counter()
    for _ in range(retries + 1):
        attempts += 1
        try:
            msg = user_msg
            resp = _request_with_backoff(
                lambda: client.chat.completions.create(
                    model=MODEL,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": msg},
                    ],
                    response_format={"type": "json_object"},
                ),
                tag,
            )
        except Exception as e:  # 网络 / 鉴权 / 限流耗尽 / 余额
            raise ReviewError(f"[{tag}] 调用模型失败:{type(e).__name__} - {e}") from e

        if usage is not None:
            u = getattr(resp, "usage", None)
            usage.add_tokens(getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))

        content = resp.choices[0].message.content
        try:
            data = _parse_json(content)
        except json.JSONDecodeError as e:
            last_err = e
            user_msg = (
                f"{user}\n\n[系统提醒] 你上一次的输出不是合法 JSON(解析错误:{e.msg})。"
                "请重新只输出合法 JSON 对象,注意引号配对与逗号分隔,不要再犯同样的语法错误。"
            )
            continue

        if usage is not None:
            usage.add_call(attempts, time.perf_counter() - started)
        return data

    raise ReviewError(f"[{tag}] 重试 {retries} 次后仍无法解析 JSON:{last_err}")


def call_text(client, system, user, usage=None, temperature=ROLE_TEMPERATURE, tag="模型"):
    """纯文本调用(不走 JSON mode)。改写 Agent 要输出完整方案全文,不适合塞进 JSON 字段。"""
    started = time.perf_counter()
    try:
        resp = _request_with_backoff(
            lambda: client.chat.completions.create(
                model=MODEL,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            ),
            tag,
        )
    except Exception as e:
        raise ReviewError(f"[{tag}] 调用模型失败:{type(e).__name__} - {e}") from e

    if usage is not None:
        u = getattr(resp, "usage", None)
        usage.add_tokens(getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))
        usage.add_call(1, time.perf_counter() - started)

    text = (resp.choices[0].message.content or "").strip()
    # 剥掉模型习惯性加上的 markdown 代码围栏
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if not text:
        raise ReviewError(f"[{tag}] 返回了空内容")
    return text


def _role_user_prompt(title, text, plan_type=""):
    """角色评审的用户侧输入:方案标题 + 正文 + 方案类型对应的关注点提醒"""
    parts = [f"产品方案标题:{title}", f"产品方案正文:\n{text}"]
    if plan_type:
        parts.append(f"(方案类型:{plan_type})")
        hint = PLAN_TYPE_HINTS.get(plan_type)
        if hint:
            parts.append(hint)
    return "\n".join(parts)


def review(
    client,
    title,
    text,
    plan_type="",
    on_role_done=None,
    roles=None,
    parallel=True,
    usage=None,
    config=None,
):
    """完整评审流程。返回 {"roles": {...}, "report": {...}, "usage": {...}, "config": "C3"}

    client: OpenAI 客户端
    title / text: 方案标题与正文
    plan_type: 方案类型(如"产品需求(PRD)"),会附加对应的评审关注点
    on_role_done: 可选回调,每完成一个评审位就调用一次(界面用来显示进度)
    roles: 参与评审的角色列表,默认全部 4 个(消融实验时传入去掉某角色的列表)
    parallel: 评审是否并发(评审位之间本来就互不依赖,并发不影响"角色隔离")
    usage: 可选的外部 Usage 对象(想把多次评审的用量累加时传入)
    config: 对照组编号("C0".."C3")。传了就完全按该配置跑,忽略 roles;
            不传则维持产品默认行为(C3:4 角色排他人设 + 主持人)。
    """
    if config is not None:
        spec = CONFIGS[config]
        reviewers, with_host = spec["reviewers"], spec["host"]
    else:
        # 产品默认用 C3(四角色 + 排他人设):对照实验里它的领域覆盖与缺陷召回最高、
        # 空泛观点率最低。实验证明了最优配置,产品就该跑最优配置。
        config = "C3"
        reviewers = roles if roles is not None else ROLES
        with_host = True

    usage = usage if usage is not None else Usage()
    wall_started = time.perf_counter()
    user_prompt = _role_user_prompt(title, text, plan_type)
    out_template = CROSS_OUTPUT_TEMPLATE

    # 评审位编号:B2 是同一个人设跑 4 遍,key 会重复,所以统一带上序号做槽位标识
    slots = [
        {"slot": f"{r['key']}_{i}" if len(reviewers) > 1 else r["key"], "role": r}
        for i, r in enumerate(reviewers, 1)
    ]
    if len({r["key"] for r in reviewers}) == len(reviewers):
        slots = [{"slot": r["key"], "role": r} for r in reviewers]  # 角色互不重复时用原 key,可读性更好

    def review_one(slot):
        data = _call_json(
            client,
            slot["role"]["persona"] + out_template,
            user_prompt,
            usage=usage,
            temperature=ROLE_TEMPERATURE,
            tag=slot["role"]["name"],
        )
        return _normalize_opinions(data)

    # 第 1 步:各评审位各自独立评审(输入只有方案原文+自己的人设,互不传对方观点)
    raw = {}
    if parallel and len(slots) > 1:
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:
            futures = {pool.submit(review_one, s): s for s in slots}
            for fut in as_completed(futures):
                slot = futures[fut]
                raw[slot["slot"]] = fut.result()  # 出错会在主线程抛出 ReviewError
                if on_role_done:
                    on_role_done(slot["role"]["key"])
    else:
        for slot in slots:
            raw[slot["slot"]] = review_one(slot)
            if on_role_done:
                on_role_done(slot["role"]["key"])

    # 按声明顺序重排:让主持人的输入与"谁先跑完"无关,保证结果可复现
    role_results = {s["slot"]: raw[s["slot"]] for s in slots}

    # 第 2 步:主持人汇总(只拿到各评审的书面意见)。B0/B1 没有主持人,报告直接由观点合成。
    if with_host:
        report = _call_json(
            client,
            _host_system(reviewers),
            "各评审的书面意见如下:\n" + json.dumps(role_results, ensure_ascii=False, indent=1),
            usage=usage,
            temperature=ROLE_TEMPERATURE,
            tag="主持人",
        )
    else:
        report = _report_without_host(role_results)

    usage.wall_seconds = time.perf_counter() - wall_started
    return {
        "roles": role_results,
        "report": report,
        "usage": usage.as_dict(),
        "config": config,
        "slots": [{"slot": s["slot"], "key": s["role"]["key"], "name": s["role"]["name"]} for s in slots],
    }


def _normalize_opinions(data):
    """把 B0 的裸字符串观点补齐成统一结构,后续指标计算就不用分两套逻辑。

    B0 不要求引用原文,所以"原文引用"补为"无" —— 这本身就是它引用有效率为 0 的原因,
    不是统计口径不公平,而是"不要求引用"这个配置的真实后果。
    """
    opinions = data.get("观点", [])
    fixed = []
    for op in opinions:
        if isinstance(op, str):
            fixed.append({"类型": "问题", "严重度": "中", "观点": op, "原文引用": "无", "建议": ""})
        elif isinstance(op, dict):
            fixed.append(op)
    return {"观点": fixed}


def _report_without_host(role_results):
    """B0/B1 没有主持人环节,返回一个空的报告骨架。

    刻意不给它伪造"共识/分歧/修改清单" —— 单个 Agent 一次输出本来就产生不了这些东西,
    编出来会让对照组失真。它的评审内容全部保留在 result["roles"] 里,
    评测时判定的是"完整评审产出"(报告 + 各评审原始观点),所以不会因此少算它的观点。
    """
    return {"共识": [], "分歧": [], "风险清单": [], "修改清单": []}


def _as_bool(value):
    """裁判偶尔会返回 "true"/"是"/1 而不是布尔,统一归一化"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "是", "覆盖", "命中")
    return False


JUDGE_SYSTEM = """你是评测裁判。给定一组"基准关键问题"(真实评审会关心的风险点),和一份 AI 生成的评审报告。
逐条判断:这份报告有没有指出同一个风险点。

判定标准:
1. 报告的 共识 / 分歧 / 风险清单 / 修改清单 里,只要存在指向同一风险的条目,即算"覆盖";
   措辞、术语、颗粒度都不要求一致,看的是"是否指向同一个风险",不是"是否用了同样的词";
2. 覆盖必须有据:你要从报告里摘录一句原文当依据;摘不出原文的,一律判为未覆盖;
3. 你只判断"有没有指出这个风险",不评价报告写得好不好、建议是否高明;
4. 不要把"报告提到了相关模块"当成覆盖 —— 必须是指出了该风险本身。

必须只输出一个 JSON 对象,格式如下,不要输出任何其他文字:
{
  "明细": [
    {"序号": 1, "是否覆盖": true, "报告依据": "从报告中摘录的原句;未覆盖时写\\"无\\"", "理由": "一句话"}
  ]
}
明细的条数与顺序必须与基准问题一一对应。"""


def eval_hit_rate(client, report, baseline_items, usage=None):
    """评测:给定"基准关键问题"列表(来自真实评审会纪要),判断 AI 报告的
    修改清单/共识/风险 覆盖了其中几条。返回命中率、命中/漏检明细,以及逐条的判定依据。

    这是产品内置的评测功能(LLM-as-judge):面试时现场粘贴一份真实纪要,
    立刻能算出命中率,回答"效果怎么衡量"。

    两个刻意的评测设计(面试可讲):
    1. 裁判固定 temperature=0 —— 评测口径必须可复现,不能每次点一下就换一个数;
    2. 裁判必须为"判定覆盖"从报告里摘出原文依据 —— 和四位评审"强制引用原文"是同一条原则:
       没有证据的判断不算判断。这样命中率是可审计的,而不是一个黑箱数字。
    """
    if not baseline_items:
        raise ReviewError("基准关键问题为空,无法评测")

    numbered = "\n".join(
        f"{i + 1}. {_render_baseline_item(item)}" for i, item in enumerate(baseline_items)
    )
    data = _call_json(
        client,
        JUDGE_SYSTEM,
        "基准关键问题:\n" + numbered + "\n\nAI 评审报告:\n" + json.dumps(report, ensure_ascii=False, indent=1),
        usage=usage,
        temperature=JUDGE_TEMPERATURE,
        tag="评测裁判",
    )

    rows = data.get("明细")
    if not isinstance(rows, list):
        # 兼容裁判偷懒直接给布尔数组的情况
        legacy = data.get("结果")
        if not isinstance(legacy, list):
            raise ReviewError("裁判输出缺少『明细』数组,无法计算命中率")
        rows = [{"是否覆盖": f, "报告依据": "", "理由": ""} for f in legacy]

    # 长度对不齐时按基准条数对齐(多的截掉、少的补未覆盖),保证命中率分母始终是基准条数
    rows = rows[: len(baseline_items)]
    rows += [{"是否覆盖": False, "报告依据": "无", "理由": "裁判未给出判定"}] * (len(baseline_items) - len(rows))

    report_text = json.dumps(report, ensure_ascii=False)
    used_evidence = set()
    detail = []
    for item, row in zip(baseline_items, rows):
        label = item["标题"] if isinstance(item, dict) else item
        evidence = str(row.get("报告依据") or "").strip()
        hit = _as_bool(row.get("是否覆盖")) and evidence not in ("", "无", "None")
        note = ""
        key = _normalize(evidence)
        if hit and not _contains(report_text, evidence):
            hit, note = False, "依据在报告原文中检索不到,按漏检处理"
        elif hit and key in used_evidence:
            hit, note = False, "该依据已用于覆盖另一条基准,不得复用,按漏检处理"
        elif hit:
            used_evidence.add(key)
        detail.append(
            {
                "基准问题": label,
                "是否覆盖": hit,
                "报告依据": evidence or "无",
                "理由": str(row.get("理由") or ""),
                "程序校验": note,
            }
        )

    flags = [d["是否覆盖"] for d in detail]
    covered = [d["基准问题"] for d in detail if d["是否覆盖"]]
    missed = [d["基准问题"] for d in detail if not d["是否覆盖"]]
    return {
        "结果": flags,
        "命中率": len(covered) / len(baseline_items),
        "命中": covered,
        "漏检": missed,
        "明细": detail,
    }


def _render_baseline_item(item):
    """基准条目支持两种写法:纯字符串(用户现场粘贴),或带命中要点/漏检情形的结构化条目。

    结构化条目把"什么算命中、什么算沾边但不算"提前写死,是消除裁判自由裁量的关键。
    """
    if not isinstance(item, dict):
        return str(item)
    parts = [item.get("标题", "")]
    if item.get("命中要点"):
        parts.append(f"   [命中要点] {item['命中要点']}")
    if item.get("漏检情形"):
        parts.append(f"   [判为漏检的情形] {item['漏检情形']}")
    return "\n".join(parts)


def _normalize(text):
    """归一化:去掉空白、引号和标点,再做子串检索。

    为什么连标点也要去掉:实测发现模型引用原文时经常截断句子并自己补一个句号
    (原文「每日凌晨批量计算,写入用户表。」被引用成「每日凌晨批量计算。」),
    留着标点会让这类合法引用被误判成无效引用,系统性压低引用有效率和召回率。
    """
    drop = " \t\r\n“”\"'‘’「」『』()()【】[]，,。.、;;:!!??~—-·/|"
    return str(text or "").translate(str.maketrans("", "", drop))


def _contains(haystack, needle):
    """依据是否真的出现在原文里。允许模型用省略号截断引用(分段全中即算命中)。

    额外设一条下限:归一化后短于 6 个字符的"依据"一律不认 —— 否则一个"降权"
    这样的词就能碰巧命中,等于把程序校验这道闸门废掉。
    """
    hay = _normalize(haystack)
    parts = [p for p in re.split(r"…+|\.{3,}", str(needle or "")) if _normalize(p)]
    if not parts or len(_normalize("".join(parts))) < 6:
        return False
    return all(_normalize(p) in hay for p in parts)


def to_markdown(title, plan_type, result):
    """把一轮评审导出成 Markdown 评审纪要(界面的"下载报告"用)。

    产品闭环的最后一步:评审结论要能带出产品、贴进 PRD,而不是只停在页面上。
    """
    report = result.get("report", {})
    usage = result.get("usage", {})
    lines = [
        f"# 评审纪要 ·《{title}》",
        "",
        f"- 方案类型:{plan_type or '通用方案'}",
        f"- 评审模型:{MODEL}",
        f"- 生成时间:{time.strftime('%Y-%m-%d %H:%M')}",
        f"- 本轮用量:{usage.get('调用次数', '-')} 次调用 / {usage.get('tokens', '-')} tokens / "
        f"约 {usage.get('成本_元', '-')} 元 / 耗时 {usage.get('耗时_秒', '-')} 秒",
        "",
        "## 一、共识",
    ]
    lines += [f"- {x}" for x in report.get("共识", [])] or ["- (无)"]
    lines += ["", "## 二、分歧"]
    lines += [f"- {x}" for x in report.get("分歧", [])] or ["- (无)"]
    lines += ["", "## 三、风险清单"]
    lines += [f"- {x}" for x in report.get("风险清单", [])] or ["- (无)"]

    lines += ["", "## 四、修改清单(按影响 × 概率排序)", "", "| 优先级 | 事项 | 依据 | 建议动作 |", "|---|---|---|---|"]
    for m in report.get("修改清单", []):
        row = [str(m.get(k, "")).replace("|", "/").replace("\n", " ") for k in ("优先级", "事项", "依据", "建议动作")]
        lines.append("| " + " | ".join(row) + " |")

    lines += ["", "## 五、各评审原始观点"]
    # 用 result["slots"] 而不是硬编码 ROLES:B2 那种"同人设多槽位"的配置也能正确导出
    slots = result.get("slots") or [{"slot": r["key"], "key": r["key"], "name": r["name"]} for r in ROLES]
    icons = {r["key"]: r["icon"] for r in ROLES}
    icons[GENERIC_ROLE["key"]] = GENERIC_ROLE["icon"]
    for s in slots:
        opinions = (result.get("roles", {}).get(s["slot"]) or {}).get("观点")
        if not opinions:
            continue
        lines += ["", f"### {icons.get(s['key'], '·')} {s['name']}"]
        for i, op in enumerate(opinions, 1):
            lines.append(f"{i}. [{op.get('类型')}/{op.get('严重度')}] {op.get('观点')}")
            for q in quotes_of(op):
                lines.append(f"   - 原文引用:{q}")
            if op.get("建议"):
                lines.append(f"   - 建议:{op.get('建议')}")
    return "\n".join(lines)
