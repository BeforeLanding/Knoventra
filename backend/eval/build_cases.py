"""Generates cases.jsonl (60 cases). Edit here, re-run, commit both files.

Split: the first N of each category (DEV_COUNTS) are the 20 tuning cases; the other 40 are held out
and must only be run for the final report.
"""
import json
from pathlib import Path

E17, PUMP, CAL, SEN, COOL, PWR = "E17 系列设备故障手册", "泵组维护手册", "校准流程（仅限中国区）", "感測器故障手冊", "冷卻系統維護手冊", "電源模組排障手冊"
VPN, LEAVE, COMP, EXP, BUD = "VPN 使用说明", "休假制度", "薪酬调整办法", "费用报销办法", "年度预算说明"

def hit(user, q, title, pages, rev=1):
    return dict(user=user, question=q, expect=dict(title=title, pages=pages, revision=rev))

ops = [
    hit("sg-ops", "E17 设备报代码 42 先查什么", E17, [2]),
    hit("sg-ops", "E17-42 主轴温度异常怎么处理", E17, [2]),
    hit("sg-ops", "E17-42 复位后仍然报警应该联系谁", E17, [2]),
    hit("sg-ops", "E17-07 进料卡滞怎么办", E17, [2]),
    hit("sg-ops", "E17 手册适用于哪些设备", E17, [1]),
    hit("sg-ops", "P03-11 泵组压力不足怎么处理", PUMP, [2]),
    hit("sg-ops", "泵组日常检查多久做一次", PUMP, [1]),
    hit("sg-ops", "SEAL-9X 是什么型号的备件", PUMP, [2]),
    hit("sg-ops", "压力不足时先检查哪个阀门", PUMP, [2]),
    hit("tw-ops", "S5-19 感測器訊號漂移怎麼排查", SEN, [2]),
    hit("tw-ops", "K08-03 冷卻液溫度過高怎麼辦", COOL, [1]),
    hit("tw-ops", "冷卻液多久更換一次", COOL, [1]),
    hit("tw-ops", "W12-30 電源模組過載怎麼處理", PWR, [1]),
    hit("tw-ops", "更換保險絲要用什麼規格", PWR, [1]),
    hit("cn-ops", "C21-05 校准过期怎么清除", CAL, [1]),
    hit("cn-ops", "中国区设备多久校准一次", CAL, [1]),
    hit("sg-ops", "E17 设备环境温度超过多少算异常", E17, [2]),
    hit("sg-ops", "泵组压力不足时是否要更换密封圈", PUMP, [2]),
    hit("sg-ops", "VPN 客户端在哪里下载", VPN, [1]),
    hit("hr", "新员工入职当天 IT 会开通什么", "IT 入职指南", [1]),
]
cross = [
    hit("tw-ops", "泵組壓力不足 P03-11 怎麼處理", PUMP, [2]),
    hit("tw-ops", "泵組日常檢查要做什麼", PUMP, [1]),
    hit("tw-ops", "E17-42 主軸溫度異常怎麼排查", E17, [2]),
    hit("tw-ops", "E17 設備代碼 42 先檢查什麼", E17, [2]),
    hit("tw-ops", "進料卡滯 E17-07 怎麼處理", E17, [2]),
    hit("sg-ops", "感测器信号漂移 S5-19 怎么排查", SEN, [2]),
    hit("sg-ops", "冷却液多久更换一次", COOL, [1]),
    hit("sg-ops", "电源模块过载 W12-30 怎么办", PWR, [1]),
    hit("sg-ops", "保险丝要用什么规格", PWR, [1]),
    hit("tw-ops", "VPN 遠端連線怎麼使用", VPN, [1]),
]
def acc(user, q, *forbidden):
    return dict(user=user, question=q, expect=dict(forbidden_titles=list(forbidden)))
access = [
    acc("hr", "E17-42 报警怎么处理", E17), acc("hr", "P03-11 泵组压力不足", PUMP),
    acc("hr", "C21-05 校准过期", CAL), acc("fin", "E17-07 进料卡滞怎么办", E17),
    acc("fin", "S5-19 感測器訊號漂移", SEN), acc("fin", "年假怎么申请", LEAVE),
    acc("sg-ops", "Ops 部门预算上限是多少", BUD), acc("sg-ops", "差旅报销时限是多久", EXP),
    acc("sg-ops", "每年什么时候调薪", COMP), acc("sg-ops", "入职满一年有几天年假", LEAVE),
    acc("sg-ops", "C21-05 校准证书过期怎么办", CAL), acc("tw-ops", "中国区设备校准证书放在哪里", CAL),
    acc("hr", "单笔超过 5000 元的报销要谁审批", EXP), acc("fin", "调薪评审结果谁能看到", COMP),
    acc("admin", "E17-42 排查步骤", E17),
]
nobasis = [dict(user="sg-ops", question=q, expect=dict(refuse=True)) for q in [
    "Z99-01 故障码怎么处理", "E17 设备的保修期是多长", "公司食堂几点开饭", "怎么给 E18 设备更换主轴",
    "P03-99 泵组报警是什么意思", "新加坡下周天气怎么样", "S5 感测器的出厂价格是多少",
    "怎么重置 K08 系列的固件", "公司现任 CEO 是谁", "W12-31 代码怎么处理"]]
esc = [
    dict(user="sg-ops", question="E17-42 复位后仍报警，帮我转人工专家", expect=dict(decisions=[True], records=1)),
    dict(user="sg-ops", question="这个问题我想转人工", expect=dict(decisions=[True, True, False], records=1)),
    dict(user="sg-ops", question="帮我转交专家处理 E17-42", expect=dict(decisions=[False], records=0)),
    dict(user="tw-ops", question="我要轉人工專家", expect=dict(decisions=[True], records=1)),
    dict(user="hr", question="我想转人工咨询休假问题", expect=dict(decisions=[False], records=0)),
]
DEV = {"ops": 7, "cross": 3, "access": 5, "no_basis": 3, "escalation": 2}
out = []
for cat, items in [("ops", ops), ("cross", cross), ("access", access), ("no_basis", nobasis), ("escalation", esc)]:
    for i, c in enumerate(items):
        out.append(dict(id=f"{cat}-{i + 1:02d}", category=cat, split="dev" if i < DEV[cat] else "holdout", **c))
assert len(out) == 60 and sum(c["split"] == "dev" for c in out) == 20
Path(__file__).with_name("cases.jsonl").write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in out) + "\n", encoding="utf-8")
print(len(out), "cases")
