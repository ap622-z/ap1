"""skill —— 行为型技能，非可调用函数。

MVP 首个 = 提问：当用户表述含糊、且该信息可能影响后续理解与判断时，
把一段指导文本注入当次 prompt，引导 agent 先巧妙反问澄清再继续。
"""

from __future__ import annotations

import re

_VAGUE_MARKERS = ("他", "她", "那个", "这个", "帮我", "你说", "怎么样", "怎么办", "什么", "吗", "呢")

QUESTION_SKILL_TAG = "[question-skill]"


def question_skill_trigger(user_text: str, context_rounds: int) -> tuple[bool, str]:
    """轻量规则判定是否命中「提问」skill。

    当用户表述含糊、且该信息可能影响后续理解与判断时，返回 (True, 注入的指导文本)。
    这里不追求 NLP，MVP 用信息缺失/含糊信号：
    - 本会话还没有任何先例（第一句）且句子很短、以代词/疑问收尾 → 极可能缺指代；
    - 提问句式但主语缺失（以"她/他/那个"开头）。
    命中即把带 tag 的指导注入当次 system prompt（tag 供观测/测试确认注入与否）。
    """
    text = (user_text or "").strip()
    if not text:
        return False, ""
    is_first = context_rounds == 0
    starts_pronoun = re.match(r"^(他|她|它|那个|这个人|这个事)", text) is not None
    short_vague = len(text) <= 12 and any(m in text for m in ("什么", "吗", "呢", "怎么样"))
    if (is_first and (starts_pronoun or short_vague)) or starts_pronoun:
        guidance = (
            f"{QUESTION_SKILL_TAG} 若用户的表述含糊、缺关键信息"
            "（如只用了“他/她/那个”，或问题缺少必要背景），先自然地反问澄清，"
            "不要自行假设关键信息；澄清后再继续。"
        )
        return True, guidance
    return False, ""
