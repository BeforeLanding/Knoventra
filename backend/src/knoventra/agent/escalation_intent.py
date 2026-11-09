"""Conservative direct routing for explicit Chinese handoff requests.

Unrecognised, quoted, negated, conditional and long requests retain the model path.
This only opens the existing confirmation workflow; it never approves a write.
"""
import re

from langchain_core.messages import HumanMessage

ESCALATION_ROUTING_POLICY = "unambiguous-chinese-request-v1"
_QUOTED = re.compile(r'```[\s\S]*?(?:```|\Z)|`[^`\n]*`|"[^"\n]*"|“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’')
_EXCLUDED = re.compile(r"如果|假如|倘若|若|万一|萬一|否则|否則|必要时|必要時|以后|以後|稍后|稍後|暂缓|暫緩|等.*再|再转|再轉|"
                       r"不要|别|別|请勿|請勿|不想|不需要|不用|不必|无需|無需|暂不|暫不|先不|不希望|取消|撤销|撤銷|"
                       r"如何|怎么|怎麼|流程|步骤|步驟|什么意思|什麼意思|为什么|為什麼|收费|收費|费用|費用|是否支持|"
                       r"他说|他說|她说|她說|有人说|有人說|用户说|用戶說|客户说|客戶說|假设|假設|例如|示例|原文|引文|"
                       r"\b(?:if|unless|later|don't|do not|cancel|how|whether|why|example)\b", re.IGNORECASE)
_REQUEST = re.compile(
    r"^(?:(?:这个|這個|当前|當前|上述)?(?:问题|問題)\s*)?"
    r"(?:(?:我)?(?:想要|想|希望|需要|要)|(?:请|請)(?:你)?(?:帮我|幫我)?|帮我|幫我|"
    r"麻烦(?:你)?(?:帮我)?|麻煩(?:你)?(?:幫我)?|能(?:否)?(?:帮我|幫我)?)?\s*"
    r"(?:(?:把|将|將)(?:这个|這個|当前|當前|上述|这|這|我的)?(?:问题|問題|情况|情況|请求|請求)\s*)?"
    r"(?:转|轉)(?:交(?:给|給)?|给|給|接)?\s*(?:人工(?:客服|专家|專家)?|专家|專家)"
    r"(?:处理|處理|一下|协助|協助|帮忙|幫忙)?(?:吧|吗|嗎)?$"
)


def direct_escalation_summary(messages) -> str | None:
    if not messages or not isinstance(messages[-1], HumanMessage):
        return None  # Tools/resume never start another direct operation in the same turn.
    text = messages[-1].content
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        return None
    unquoted = _QUOTED.sub("", text)
    if _EXCLUDED.search(unquoted):
        return None
    clauses = re.split(r"[，,。.!！?？;；:：\n]", unquoted)
    if any(_REQUEST.fullmatch(clause.strip()) for clause in clauses if clause.strip()):
        prior = [m.content for m in messages[:-1] if isinstance(m, HumanMessage)]
        if any(not isinstance(value, str) for value in prior):
            return None
        summary = text.strip()
        if prior:
            statements = "\n".join(f"{index}. {value}" for index, value in enumerate(prior, 1))
            summary = f"已有用户陈述：\n{statements}\n当前转交请求：\n{summary}"
        # Preserve all user context or let the model summarise it; never silently drop details.
        return summary if len(summary) <= 2000 else None
    return None
