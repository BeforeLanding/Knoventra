"""Generates cases_v2.jsonl: a validation set written AFTER the holdout analysis, for checking the gate and
refusal fixes. Cases target exact fault codes / part numbers (the weakness found on holdout) and near-miss
codes that must be refused. They are split "val2"; do not tune on them and then report them as final.
"""
import json
from pathlib import Path

E17, PUMP, CAL, SEN, COOL, PWR = "E17 系列设备故障手册", "泵组维护手册", "校准流程（仅限中国区）", "感測器故障手冊", "冷卻系統維護手冊", "電源模組排障手冊"
VPN, LEAVE, COMP, EXP = "VPN 使用说明", "休假制度", "薪酬调整办法", "费用报销办法"


def hit(user, q, title, pages):
    return dict(category="ops", user=user, question=q, expect=dict(title=title, pages=pages, revision=1))


def no(q, user="sg-ops"):
    return dict(category="no_basis", user=user, question=q, expect=dict(refuse=True))


cases = [
    hit("sg-ops", "E17-07 报警是什么原因", E17, [2]),
    hit("sg-ops", "P03-11 报警含义", PUMP, [1, 2]),
    hit("cn-ops", "C21-05 是什么故障", CAL, [1]),
    hit("tw-ops", "S5-19 是什麼故障", SEN, [1, 2]),
    hit("tw-ops", "SNS-5B 用在哪裡", SEN, [2]),
    hit("tw-ops", "K08-03 是什麼警報", COOL, [1]),
    hit("tw-ops", "W12-30 是什麼故障碼", PWR, [1]),
    hit("tw-ops", "15A 慢熔型保險絲", PWR, [1]),
    hit("sg-ops", "SEAL-9X", PUMP, [2]),
    hit("sg-ops", "E17-42", E17, [1, 2]),
    hit("sg-ops", "P03-11", PUMP, [1, 2]),
    hit("sg-ops", "35 摄氏度 E17 环境温度", E17, [2]),
    hit("hr", "年假 15 天 提前几天申请", LEAVE, [1]),
    hit("fin", "5000 元 审批 报销", EXP, [1]),
    hit("hr", "4 月 调薪 评审", COMP, [1]),
    hit("sg-ops", "VPN 客户端", VPN, [1]),
    # near-miss codes / part numbers that do not exist, and clearly off-topic questions
    no("E17-99 报警是什么原因"), no("P03-12 是什么故障"), no("S5-20 怎么处理", "tw-ops"),
    no("K08-04 冷却液温度", "tw-ops"), no("W12-31 过载怎么办", "tw-ops"), no("SEAL-9Y 是什么备件"),
    no("E18-42 主轴温度异常"), no("C21-06 校准过期", "cn-ops"),
    no("公司股票代码是多少"), no("下周班车时间表"), no("如何申请出差签证"), no("Python 怎么读取文件"),
]
out = [dict(id=f"v2-{c['category']}-{i + 1:02d}", split="val2", **c) for i, c in enumerate(cases)]
Path(__file__).with_name("cases_v2.jsonl").write_text(
    "\n".join(json.dumps(c, ensure_ascii=False) for c in out) + "\n", encoding="utf-8")
print(len(out), "cases")
