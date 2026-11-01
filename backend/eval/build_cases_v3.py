"""cases_v3.jsonl: written BEFORE the gate was re-calibrated, and not scored until the gate is frozen.
Calibrate on dev + val2 only; run val3 once as the out-of-sample check for the gate/refusal fixes.
"""
import json
from pathlib import Path

E17, PUMP, CAL, SEN, COOL, PWR = "E17 系列设备故障手册", "泵组维护手册", "校准流程（仅限中国区）", "感測器故障手冊", "冷卻系統維護手冊", "電源模組排障手冊"
VPN, LEAVE, BUD, ONB = "VPN 使用说明", "休假制度", "年度预算说明", "IT 入职指南"


def hit(user, q, title, pages):
    return dict(category="ops", user=user, question=q, expect=dict(title=title, pages=pages, revision=1))


def no(q, user="sg-ops"):
    return dict(category="no_basis", user=user, question=q, expect=dict(refuse=True))


cases = [
    hit("sg-ops", "E17-07 怎么复位", E17, [2]),
    hit("sg-ops", "泵组 密封圈 SEAL-9X 更换", PUMP, [2]),
    hit("sg-ops", "P03-11 压力不足 滤网", PUMP, [2]),
    hit("cn-ops", "C21-05 证书编号 输入控制台", CAL, [1]),
    hit("cn-ops", "校准证书存档在哪里", CAL, [1]),
    hit("tw-ops", "S5-19 零點校正", SEN, [2]),
    hit("tw-ops", "感測器接頭鬆動", SEN, [2]),
    hit("tw-ops", "K08-03 散熱風扇", COOL, [1]),
    hit("tw-ops", "冷卻液 排空管路 更換", COOL, [1]),
    hit("tw-ops", "W12-30 拔除非必要負載", PWR, [1]),
    hit("tw-ops", "保險絲 禁止使用其他規格", PWR, [1]),
    hit("sg-ops", "E17 冷却液 液位 刻度线", E17, [2]),
    hit("hr", "带薪年假 HR 系统 申请", LEAVE, [1]),
    hit("fin", "预算 11 月 提交", BUD, [1]),
    hit("sg-ops", "初始密码 首次登录", ONB, [1]),
    hit("sg-ops", "远程办公 VPN 接入", VPN, [1]),
    no("E17-43 报警"), no("P03-10 压力"), no("S5-18 漂移", "tw-ops"), no("K08-02 温度", "tw-ops"),
    no("W12-29 过载", "tw-ops"), no("SEAL-9Z 密封圈"), no("C21-04 校准", "cn-ops"), no("E19-42 温度"),
    no("公司年会时间"), no("怎么订会议室"), no("同事的工资是多少"), no("笔记本电脑报修流程"),
]
out = [dict(id=f"v3-{c['category']}-{i + 1:02d}", split="val3", **c) for i, c in enumerate(cases)]
Path(__file__).with_name("cases_v3.jsonl").write_text(
    "\n".join(json.dumps(c, ensure_ascii=False) for c in out) + "\n", encoding="utf-8")
print(len(out), "cases")
