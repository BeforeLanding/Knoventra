"""Targeted regression set for exact-identifier policy v1.

Written after the val3 diagnosis. These are policy-focused cases on the same
small fictional corpus, not an independent estimate of general QA quality.
Run real-model retrieval once after the policy is frozen; keep older results.
"""
import json
from pathlib import Path

E17, PUMP, CAL = "E17 系列设备故障手册", "泵组维护手册", "校准流程（仅限中国区）"
SEN, COOL, PWR, VPN = "感測器故障手冊", "冷卻系統維護手冊", "電源模組排障手冊", "VPN 使用说明"


def hit(user, question, title, pages, category="ops"):
    return dict(category=category, user=user, question=question,
                expect=dict(title=title, pages=pages, revision=1))


def no(question, user="sg-ops"):
    return dict(category="no_basis", user=user, question=question, expect=dict(refuse=True))


def access(user, question, title):
    return dict(category="access", user=user, question=question, expect=dict(forbidden_titles=[title]))


cases = [
    hit("sg-ops", "E17-42 报警后液位应该检查哪里", E17, [2]),
    hit("sg-ops", "E17-07 进料口卡滞时应如何断电处理", E17, [2]),
    hit("sg-ops", "P03-11 压力异常的滤网检查顺序", PUMP, [2]),
    hit("sg-ops", "更换 SEAL-9X 密封圈前要检查什么", PUMP, [2]),
    hit("cn-ops", "C21-05 校准过期后证书该怎么登记", CAL, [1]),
    hit("tw-ops", "S5-19 異常時接頭和零點如何排查", SEN, [2]),
    hit("tw-ops", "SNS-5B 感測器在什麼情況下更換", SEN, [2]),
    hit("tw-ops", "K08-03 發生時冷卻液液位如何處理", COOL, [1]),
    hit("tw-ops", "W12-30 電源過載後負載如何處置", PWR, [1]),
    hit("hr", "远程办公连接 VPN 应走什么流程", VPN, [1]),
    hit("tw-ops", "ｅ１７－４２ 的冷卻液液位要查哪裡", E17, [2], "cross"),
    hit("tw-ops", "s5‑19 感測器接頭鬆動怎麼辦", SEN, [2], "cross"),
    hit("sg-ops", "seal−9x 密封圈更换前的检查", PUMP, [2], "cross"),
    hit("tw-ops", "K08 – 03 過熱時散熱風扇怎麼查", COOL, [1], "cross"),
    no("E17-46 主轴报警该怎么停机"),
    no("P03-13 泵组压力报警的处理步骤"),
    no("SEAL-9V 密封圈的更换方法"),
    no("SNS-5D 感測器接頭怎麼檢查", "tw-ops"),
    no("K08-06 冷卻液過熱的處置", "tw-ops"),
    no("W12-33 電源模組過載怎麼辦", "tw-ops"),
    no("C21-08 校准证书过期怎么办", "cn-ops"),
    no("E17-42B 的主轴报警处理"),
    no("比较 E17-42 和 E17-46 的处置差异"),
    no("比較 S5-19 和 SNS-5D 的更換流程", "tw-ops"),
    access("sg-ops", "C21-05 校准登记流程", CAL),
    access("tw-ops", "c21‑05 校準證書存放位置", CAL),
    access("hr", "E17-42 报警后的复位步骤", E17),
    access("fin", "SNS-5B 感测器如何更换", SEN),
]

if __name__ == "__main__":
    rows = [dict(id=f"v4-{c['category']}-{i + 1:02d}", split="val4", **c) for i, c in enumerate(cases)]
    Path(__file__).with_name("cases_v4.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in rows) + "\n", encoding="utf-8")
    print(len(rows), "cases")
