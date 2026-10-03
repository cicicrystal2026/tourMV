"""LLM 提示词与降级载荷。

设计原则：每个 LLM 任务都配一份「结构等价」的本地兜底产出。
未配置 Key 时流程照常推进，返回内容带 degraded=True，前端会显示「模拟」标记，
避免实验室里把占位数据误当成模型结果。
"""
from __future__ import annotations

import hashlib
import random

# ------------------------------------------------------------------ 结构常量
SECTION_PLAN = [
    ("主歌1", 4), ("预副歌", 2), ("副歌", 4),
    ("主歌2", 4), ("副歌", 4), ("桥段", 2), ("尾副歌", 4),
]

_OPENERS = [
    "{d}的清晨 雾还没散", "一条老街 慢慢醒来", "山脊上的光 落在肩上",
    "风从{img}那边 吹过来", "石板路很长 影子很短",
]
_IMAGERY = [
    "江水绕过 千年的城", "檐角的铜铃 还在响", "云在{l}上 停了一会儿",
    "灯火把夜晚 烫出一个洞", "谁的脚步 惊起一群白鹭",
]
_HOOKS = [
    "我想带你去{d} 看一次日落", "把{d}的风 装进我的口袋",
    "如果远方有名字 那一定叫{d}", "在{d}的夜里 我们慢慢走",
]
_BRIDGE = ["时间在这里 走得很慢", "所有的路 都通向这里"]


def _rnd(seed_text: str) -> random.Random:
    return random.Random(int(hashlib.md5(seed_text.encode("utf-8")).hexdigest()[:8], 16))


def _pick(pool: list[str], rnd: random.Random, n: int) -> list[str]:
    out: list[str] = []
    while len(out) < n:
        out.extend(rnd.sample(pool, min(len(pool), n - len(out))))
    return out


# ------------------------------------------------------------------ 1 歌词生成
def lyrics_messages(destination: str, genre: str, mood: list[str], duration_s: int,
                    style_prompt: str, forbidden: str = "", kb_text: str = "") -> list[dict]:
    sys = (
        "你是中国文旅原创歌曲的词作者。写一首中文歌词，要求：\n"
        "1. 每行 6-12 个汉字，押韵自然，避免生造词；\n"
        "2. 结构按段落输出，段落名用：主歌1/预副歌/副歌/主歌2/桥段/尾副歌；\n"
        "3. 副歌要有一句能被记住的记忆点；\n"
        "4. 只写原创内容，不得引用任何已有歌曲的歌词、歌名或艺人名；\n"
        "5. 地名与景物要自然融入句子，不要把「省市·景区」这种带间隔号的整串目的地名原样塞进歌词，"
        "不要写「我想带你去××」「如果远方有名字」这类可套用任何城市的模板句；\n"
        "6. 只写你确信真实的地理、历史与文化细节；拿不准的宁可写意，不要编造年代、名称或典故；\n"
        "7. 只输出 JSON：{\"sections\":[{\"name\":\"主歌1\",\"lines\":[\"…\"]}]}"
    )
    user = (
        f"目的地：{destination}\n曲风：{genre}\n情绪：{'、'.join(mood) or '治愈、辽阔'}\n"
        f"目标时长：{duration_s} 秒\n风格提示词参考：{style_prompt or '（无）'}\n"
        f"禁用内容：{forbidden or '（无）'}\n"
        "地理与文化描述必须真实可考，不要虚构地名或历史事件。"
        + (f"\n\n以下是已核实的目的地资料，写具体地名、史实、典故时只使用这里有的内容：\n{kb_text}"
           if kb_text else "")
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def lyrics_fallback(destination: str, mood: list[str]) -> dict:
    d = destination or "这里"
    rnd = _rnd(d + "".join(mood))
    sections: list[dict] = []
    for name, n in SECTION_PLAN:
        if name.startswith("副歌") or name == "尾副歌":
            pool = [h.format(d=d) for h in _HOOKS] + _IMAGERY
        elif name == "桥段":
            pool = list(_BRIDGE) + _OPENERS
        else:
            pool = list(_OPENERS) + list(_IMAGERY)
        lines = [ln.format(d=d, img=rnd.choice(["山", "水", "田"]), l=rnd.choice(["山", "湖", "塔"]))
                 for ln in _pick(pool, rnd, n)]
        sections.append({"name": name, "lines": lines})
    return {"sections": sections, "mock": True}


# ------------------------------------------------------------------ 2 重写选中行
def rewrite_messages(lines: list[str], destination: str, genre: str, instr: str) -> list[dict]:
    sys = ("你是中文歌词编辑。改写用户选中的歌词行：保持原意与字数（6-12 字）、押原有的韵脚，"
           "只输出 JSON：{\"lines\":[\"新行1\",\"新行2\"]}，行数必须与原行数一致。")
    user = f"目的地：{destination}\n曲风：{genre}\n要求：{instr or '换一种更自然的表达'}\n原行：\n" + "\n".join(lines)
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def rewrite_fallback(lines: list[str]) -> dict:
    alt = [("轻轻", "慢慢"), ("走过", "路过"), ("远方", "天边"), ("日落", "晚霞"),
           ("风", "风"), ("夜", "傍晚"), ("老街", "旧巷"), ("一", "半")]
    out = []
    for ln in lines:
        new = ln
        for a, b in alt:
            if a in new and a != b:
                new = new.replace(a, b, 1)
                break
        else:
            new = ln[:1] + "又" + ln[1:] if len(ln) < 12 else ln
        out.append(new)
    return {"lines": out, "mock": True}


# ------------------------------------------------------------------ 3 自动检查
def check_messages(sections: list[dict], destination: str, kb_text: str = "") -> list[dict]:
    """押韵与史实检查（字数由规则计算，不交给 LLM —— 它数不准中文字数）。"""
    sys = (
        "你是中文歌词审校。只做两项检查：\n"
        "rhyme：同一段内句尾韵脚明显不协调的行；\n"
        "fact：地名、历史、文化表述中你**有把握判断为错误或高度存疑**的行。"
        "存疑但不确定的，evidence 写明「需人工核实」及理由，不要断言对错；"
        "真实存在的地名、物产不要因为你不熟悉就判错。\n"
        "suggestions 必须是可直接替换原行的**完整一句歌词**：6-12 个汉字、保留原意与意象、"
        "与上下句押韵；不得截断原句，不得只改最后一个字凑韵；给不出合格改写就返回空数组。\n"
        "只输出 JSON：{\"issues\":[{\"type\":\"rhyme|fact\",\"section\":\"主歌1\",\"line_no\":3,"
        "\"text\":\"原行\",\"evidence\":\"依据\",\"suggestions\":[\"完整改写1\",\"完整改写2\"]}]}。"
        "line_no 从 1 开始、按段落内计数。没有问题的行不要输出。"
        + ("\n下面给出已核实资料（带编号）。判断史实时以资料为准：歌词与资料矛盾的必须指出，"
           "并在 kb 字段里写上依据的资料编号（如 [2]）；资料里没有覆盖的内容才写「需人工核实」。"
           "issues 每项可多一个字段 \"kb\": [编号]。" if kb_text else "")
    )
    body = "\n".join(f"[{s['name']}]\n" + "\n".join(f"{i}. {ln}" for i, ln in enumerate(s.get("lines", []), 1))
                     for s in sections)
    user = f"目的地：{destination}\n歌词：\n{body}"
    if kb_text:
        user += f"\n\n已核实资料：\n{kb_text}"
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def meter_fix_messages(items: list[dict], destination: str) -> list[dict]:
    """把字数越界的行整句改写到 6-12 字（替代 V1.0 的「截断到 8 / 10 字」）。"""
    sys = ("你是中文歌词编辑。把每一行改写为 6-12 个汉字的完整句子（不计标点），"
           "保留原意、意象与句尾韵脚，读起来要像歌词而不是摘要。不得截断原句。"
           "每行给 2 个改写。只输出 JSON：{\"items\":[{\"id\":0,\"suggestions\":[\"…\",\"…\"]}]}")
    body = "\n".join(f"{it['id']}. （{it['section']}）{it['text']}" for it in items)
    return [{"role": "system", "content": sys},
            {"role": "user", "content": f"目的地：{destination}\n需要改写的行：\n{body}"}]


_PUNCT_TAIL = "，。、；：！？,.!?;:·…—-“”\"'（）() "
# 近韵合并（十三辙的粗略版）：eng/ing/ong 同辙、en/in/un 同辙
_RHYME_GROUP = {"ing": "eng", "ong": "eng", "in": "en", "un": "en", "vn": "en", "ui": "ei"}


def _tail_char(line: str) -> str:
    t = (line or "").rstrip(_PUNCT_TAIL)
    if len(t) > 1 and t[-1] == "儿":     # 儿化：「一会儿」的韵落在「会」上
        t = t[:-1]
    return t[-1] if t else ""


def rhyme_key(line: str) -> str:
    """句尾字的韵（去介音 i/u/ü 后的韵母，近韵合并）。没有 pypinyin 时退化为句尾字本身。"""
    ch = _tail_char(line)
    if not ch:
        return ""
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        return ch
    f = pinyin(ch, style=Style.FINALS, strict=False)[0][0] or ch
    # 去介音（ian→an、uang→ang、iong→ong），但 in/ing/un/ui 这类本身就是韵母的不动
    if len(f) > 1 and f[0] in "iuv" and any(c in "aeiouv" for c in f[1:]) and f not in ("ui", "iu"):
        f = f[1:]
    return _RHYME_GROUP.get(f, f)


_PLACE_WORDS = ["塔", "寺", "桥", "城墙", "古道", "码头", "书院", "牌坊", "码头", "石窟"]
_FACT_MARKERS = ["年", "代", "千", "百", "第一", "最"]


def check_fallback(sections: list[dict], destination: str) -> dict:
    """LLM 不可用时的本地规则兜底：只标出疑点，**不给改写建议**。

    V1.0 的兜底会「截断到 8/10 字」「把最后一个字换成韵脚字」，产出
    「雾还没江」「我想带你去桂林·」这种不成句的建议 —— 宁可不给，让人来改。
    字数检查已由 lyrics.run_checks 用规则单独计算，这里只管押韵与史实。
    """
    issues: list[dict] = []
    all_lines: list[tuple[str, int, str]] = []
    for s in sections:
        for i, ln in enumerate(s.get("lines", []), 1):
            all_lines.append((s["name"], i, ln))

    # rhyme：取副歌各行尾字作为基准韵（只提示，不自动改）
    chorus = [ln for n, _, ln in all_lines if "副歌" in n]
    if chorus:
        keys = [rhyme_key(ln) for ln in chorus if ln.strip()]
        base = max(set(keys), key=keys.count) if keys else ""
        # 中文歌押韵本来就常隔句押（偶数句押韵），只检查段落里的偶数句，不逐句要求
        for name, i, ln in all_lines:
            if (base and ln.strip() and i % 2 == 0 and name in ("主歌1", "主歌2", "预副歌")
                    and rhyme_key(ln) != base):
                issues.append({
                    "type": "rhyme", "section": name, "line_no": i, "text": ln,
                    "evidence": f"副歌主韵为「{base}」，本行句尾「{_tail_char(ln)}」不押（规则检查，供参考）",
                    "suggestions": [],
                })

    # fact：含地标或年代表述的行需要人工确认
    for name, i, ln in all_lines:
        if any(w in ln for w in _PLACE_WORDS) or any(m in ln for m in _FACT_MARKERS):
            issues.append({
                "type": "fact", "section": name, "line_no": i, "text": ln,
                "evidence": f"涉及「{destination or '目的地'}」的具体地理/历史表述，需核对公开资料",
                "suggestions": [],
            })
    return {"issues": issues[:12], "mock": True}


# ------------------------------------------------------------------ 4 简报匹配分
def fit_messages(brief: dict, cand: dict) -> list[dict]:
    # 不提供模型名：匹配分只看客观参数；理由里也不能出现模型/厂商名（盲听约束）
    sys = ("你是音乐 A&R。根据创作简报与候选曲目的客观参数，给出 0-100 的匹配分与一句话理由。"
           "理由中不要提及任何模型、厂商或品牌名。只输出 JSON：{\"score\":85,\"reason\":\"…\"}")
    user = (f"简报：曲风={brief.get('genre')} 情绪={brief.get('mood')} "
            f"BPM区间={brief.get('bpm_min')}-{brief.get('bpm_max')} 时长={brief.get('duration_s')}s\n"
            f"候选：BPM={cand.get('bpm')} 时长={cand.get('duration_s')}s "
            f"声线={cand.get('vocal')}")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def fit_fallback(brief: dict, cand: dict) -> dict:
    score = 60
    reasons: list[str] = []
    lo, hi = brief.get("bpm_min") or 70, brief.get("bpm_max") or 100
    bpm = float(cand.get("bpm") or 0)
    if bpm:
        if lo <= bpm <= hi:
            score += 18
            reasons.append(f"BPM {bpm:.0f} 落在目标区间")
        else:
            score -= min(18, int(abs(bpm - (lo + hi) / 2) * 0.9))
            reasons.append(f"BPM {bpm:.0f} 偏离目标区间")
    target = float(brief.get("duration_s") or 180)
    dur = float(cand.get("duration_s") or 0)
    if dur and target:
        ratio = min(dur, target) / max(dur, target)
        score += int(ratio * 18)
        if ratio >= 0.9:
            reasons.append("时长贴合")
    if (cand.get("vocal") or "") == (brief.get("vocal") or ""):
        score += 4
        reasons.append("声线符合")
    return {"score": max(0, min(100, score)), "reason": "；".join(reasons) or "按参数估算", "mock": True}


# ------------------------------------------------------------------ 5 画面描述（粗剪用）
_SCENE_MAP: list[tuple[tuple[str, ...], list[str]]] = [
    (("江", "河", "湖", "海", "水", "浪", "波"), ["江河", "航拍"]),
    (("山", "峰", "岭", "峡", "岳"), ["山川", "航拍"]),
    (("塔", "寺", "庙", "古", "殿"), ["古塔", "寺院", "固定"]),
    (("夜", "星", "灯", "月"), ["夜晚", "延时"]),
    (("黄昏", "夕阳", "落日", "晚霞"), ["黄昏", "慢摇"]),
    (("清晨", "晨", "雾", "晓"), ["清晨", "雾", "慢推"]),
    (("花", "春", "绿", "柳"), ["花木", "春", "慢推"]),
    (("雪", "冬", "冰"), ["雪原", "冬"]),
    (("街", "巷", "镇", "市", "路"), ["老街", "古镇", "街景"]),
    (("人", "你", "我", "谁", "笑", "走"), ["人物", "慢推"]),
    (("风", "云", "天", "空"), ["天空", "延时"]),
    (("田", "村", "家", "稻"), ["梯田", "村落"]),
]


def scene_fallback(line: str, destination: str) -> str:
    tags: list[str] = []
    for keys, vals in _SCENE_MAP:
        if any(k in line for k in keys):
            tags.extend(vals)
    if not tags:
        tags = ["山水", "慢推"]
    out: list[str] = []
    for t in tags:
        if t not in out:
            out.append(t)
    if destination:
        out.append(destination)
    return " ".join(out[:4])


def scene_messages(lines: list[str], destination: str, genre: str) -> list[dict]:
    sys = ("你是 MV 视觉导演。为每句歌词写一句可检索的画面描述（10-20 字），"
           "用于在实拍素材库中做语义检索。描述要具体、可拍摄、不含歌词原文。"
           "只输出 JSON：{\"scenes\":[{\"line\":1,\"desc\":\"黄昏 古塔 慢推 暖色调\"}]}")
    user = (f"目的地：{destination}\n曲风：{genre}\n歌词行：\n" +
            "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines, 1)))
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


# ------------------------------------------------------- 6b 文生图提示词
def image_prompts_messages(destination: str, theme: str, count: int) -> list[dict]:
    """把目的地拆成若干**真实可考**的景点，并为每个写一条摄影化生图提示词。

    「真实可考」这条最关键：文旅内容不能虚构地名，否则词曲和画面会一起跑偏。
    """
    sys = (
        "你是文旅 MV 的视觉导演兼摄影指导。为指定目的地挑选真实存在的景观，"
        "并为每个景观写一条用于文生图的提示词。要求：\n"
        f"1. 恰好输出 {count} 个景观，必须真实可考，禁止虚构地名或历史事件；\n"
        "2. 覆盖不同时间（清晨/黄昏/夜晚）与不同景别（航拍/中景/近景/特写），避免雷同；\n"
        "3. 提示词用中文，包含 主体景观 + 时间光线 + 天气 + 机位景别 + 画面质感，"
        "30-60 字，写成摄影作品描述；不要出现文字、水印、正脸人像，不要选碑刻、牌匾等以文字为主体的景观；\n"
        "4. 每个景观再给 2-4 个中文关键词，供后续检索匹配用。\n"
        '只输出 JSON：{"shots":[{"place":"故宫角楼","prompt":"…","keywords":["黄昏","古建","倒影"]}]}'
    )
    user = (f"目的地：{destination}\n"
            f"主题方向：{theme or '风光与人文'}\n"
            f"需要 {count} 个景观。")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def image_prompts_fallback(destination: str, count: int) -> dict:
    d = destination or "这里"
    base = [
        ("{d}·晨景", "清晨薄雾中的{d}，柔和侧光，广角航拍，青灰色调，高清摄影，电影质感"),
        ("{d}·地标", "{d}的标志性建筑，黄金时刻暖光，中景仰视，天空留白，高清摄影"),
        ("{d}·街巷", "{d}的老街巷，雨后湿润石板路，暖色灯笼，人文纪实，浅景深"),
        ("{d}·山水", "{d}的江河与远山，蓝调时刻，长焦压缩，层次分明，高清摄影"),
        ("{d}·夜色", "{d}的夜市灯火，夜晚，霓虹与暖光交织，慢门，城市人文"),
        ("{d}·细节", "{d}的地方风物细节，特写，侧逆光，质感清晰，浅景深"),
    ]
    kinds = ["清晨", "地标", "街巷", "山水", "夜色", "细节"]
    shots = []
    for i in range(max(1, count)):
        name_tpl, body_tpl = base[i % len(base)]
        shots.append({"place": name_tpl.format(d=d),
                      "prompt": body_tpl.format(d=d),
                      "keywords": [d, kinds[i % len(kinds)]]})
    return {"shots": shots, "mock": True}


# ------------------------------------------------- 6c 逐句分镜（一句一图）
def storyboard_messages(lines: list[dict], destination: str, genre: str = "") -> list[dict]:
    """把**每一句歌词**映射到一个**真实可考的景观**，并给出生图提示词。

    这是「一句歌词 = 一张景区图」的核心一步。三条硬要求：

    1. **每行都要有**，行号与输入一一对应，不许合并、不许漏；
    2. 景观必须真实存在（具体到建筑/地物/名胜），**禁止虚构地名**；
    3. 同一首歌里尽量用不同景观（重复出现的副歌除外 —— 副歌重复时复用同一景观，
       视觉上形成呼应，这也是 MV 的常规做法）。
    """
    sys = (
        "你是文旅 MV 的视觉导演兼摄影指导。用户会给你一首歌的歌词（逐行编号），"
        "请为**每一行**挑一个真实存在的景观作为这一句的画面，并写成摄影提示词。\n"
        "要求：\n"
        "1. 输出行数必须与输入完全一致，按行号一一对应，不要合并或省略任何一行；\n"
        "2. place 必须是真实可考的景观名（要具体，如「故宫角楼」「永定门城楼」，"
        "不要写「古城」「山水」这种泛指），禁止虚构地名与史实；\n"
        "3. 同一首歌尽量避免重复用同一个景观；但**重复出现的副歌要用同一个景观**；\n"
        "4. prompt 用中文，写成摄影作品描述：主体景观 + 时间光线 + 天气 + 机位景别 + 画面质感，"
        "30-60 字；不要出现文字、水印、正脸人像；\n"
        "   **不要选以文字为主体的景观**（碑刻、石刻题字、牌匾、招牌、对联等）：生图模型写不出正确的汉字/满文，"
        "会生成乱码假字，放进文旅宣传就是史实错误。歌词提到名称、题字时，改用该处的地貌或建筑全景表现；\n"
        "5. why 用一句话说明「这句歌词为什么配这个景观」，供人工审核时快速判断；\n"
        "6. keywords 给 2-4 个中文意象词。\n"
        '只输出 JSON：{"shots":[{"line":1,"place":"…","prompt":"…","keywords":["…"],"why":"…"}]}'
    )
    body = "\n".join(
        f"{i}. [{l.get('section') or ''}] {l.get('text') or ''}"
        for i, l in enumerate(lines, 1)
    )
    user = (f"目的地：{destination}\n曲风：{genre or '不限'}\n"
            f"共 {len(lines)} 行歌词：\n{body}")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def storyboard_fallback(lines: list[dict], destination: str) -> dict:
    """无 LLM 时的兜底：按行循环套用通用机位模板，保证流程不断。"""
    d = destination or "这里"
    tpl = [
        ("{d}的标志性建筑", "{d}的标志性建筑，黄金时刻暖光，中景仰视，天空留白"),
        ("{d}的山川远景", "{d}的山川远景，蓝调时刻，长焦压缩，层次分明"),
        ("{d}的老街巷", "{d}的老街巷，雨后湿润石板路，暖色灯笼，人文纪实"),
        ("{d}的流水与石岸", "{d}的流水与石岸，清晨薄雾，低机位，柔光"),
        ("{d}的檐角细节", "{d}的檐角与砖雕细节，侧逆光，特写，质感清晰"),
        ("{d}的夜色灯火", "{d}的夜色灯火，慢门，暖冷对比，城市人文"),
    ]
    shots = []
    for i, l in enumerate(lines, 1):
        name_tpl, body_tpl = tpl[(i - 1) % len(tpl)]
        shots.append({
            "line": i,
            "place": name_tpl.format(d=d),
            "prompt": body_tpl.format(d=d),
            "keywords": [d],
            "why": "兜底模板（未配置 LLM）",
        })
    return {"shots": shots, "mock": True}



# ------------------------------------------------------- 6c 素材语义分配
def assign_assets_messages(scenes: list[dict], assets: list[dict]) -> list[dict]:
    """让 LLM 把每个画面描述分配到最贴切的一条素材上。

    为什么不用向量检索：项目里 CLIP 未安装时会回退成 md5 哈希当向量，
    余弦相似度没有语义含义，等于随机配图。用 LLM 直接做语义分配更可靠。
    """
    sys = (
        "你是 MV 剪辑师。你会拿到一组「歌词画面描述」和一组「素材描述」，"
        "请为每个画面描述挑选最贴切的一条素材。要求：\n"
        "1. 只在给定素材编号里选，不要编造编号；\n"
        "2. 尽量让相邻画面使用不同素材（同一支片子里避免重复用同一条）；\n"
        "3. 语义优先于数量均衡 —— 宁可重复也要贴合画面内容。\n"
        '只输出 JSON：{"picks":[{"line":1,"asset":3}]}'
    )
    user = (
        "画面描述：\n"
        + "\n".join(f"{i}. {s['desc']}" for i, s in enumerate(scenes, 1))
        + "\n\n候选素材：\n"
        + "\n".join(f"{i}. {a['desc']}" for i, a in enumerate(assets, 1))
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


# ------------------------------------------------------------------ 6 字幕翻译
def translate_messages(lines: list[str]) -> list[dict]:
    sys = ("你是中译英字幕译者。把每句中文歌词译成适合做短视频字幕的英文，简短、可读、"
           "保留画面感。只输出 JSON：{\"lines\":[\"…\"]}，行数必须一致。")
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n".join(lines)}]


_EN_WORDS = {
    "山": "mountains", "水": "water", "夜": "night", "风": "wind", "光": "light",
    "日落": "sunset", "远方": "far away", "走": "walk", "老街": "old street",
    "江": "river", "塔": "pagoda", "云": "clouds", "灯": "lanterns", "海": "sea",
    "花": "blossoms", "星": "stars", "路": "road", "城": "town", "桥": "bridge",
}


def translate_fallback(lines: list[str]) -> dict:
    out = []
    for ln in lines:
        words = [en for zh, en in _EN_WORDS.items() if zh in ln]
        out.append(" ".join(words[:4]).capitalize() if words else "Here we go")
    return {"lines": out, "mock": True}
