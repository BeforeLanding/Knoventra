"""New support-anchored cases for a paired rerank-off / rerank-on comparison.

Freeze these before scoring either configuration. Keep val4 unchanged, including
its diagnosed label issue. The literal support anchors check the target chunk,
not merely a document/page; they are not a human answer-entailment judgement.
"""
import json
from pathlib import Path

E17, PUMP, CAL = "E17 系列设备故障手册", "泵组维护手册", "校准流程（仅限中国区）"
SEN, COOL, PWR = "感測器故障手冊", "冷卻系統維護手冊", "電源模組排障手冊"
VPN, ONB, LEAVE, COMP, EXP, BUD = "VPN 使用说明", "IT 入职指南", "休假制度", "薪酬调整办法", "费用报销办法", "年度预算说明"


def hit(user, question, title, page, support, category="ops"):
    return dict(category=category, user=user, question=question,
                expect=dict(title=title, pages=[page], revision=1, support_text=support))


def no(user, question):
    return dict(category="no_basis", user=user, question=question, expect=dict(refuse=True))


def access(user, question, title):
    return dict(category="access", user=user, question=question, expect=dict(forbidden_titles=[title]))


cases = [
    hit("sg-ops", "E17-07 进料卡住后，清理前要做什么", E17, 2, "断电后手动清理进料口"),
    hit("sg-ops", "E17-42 复位之后需观察多少分钟", E17, 2, "复位设备并观察 10 分钟"),
    hit("sg-ops", "P03-11 进水阀应处于什么状态", PUMP, 2, "检查进水阀是否全开"),
    hit("sg-ops", "压力仍不足时，SEAL-9X 用于更换哪个部件", PUMP, 2, "更换密封圈"),
    hit("tw-ops", "SNS-5B 是在感測器持續異常時更換嗎", SEN, 2, "仍異常則更換感測器"),
    hit("tw-ops", "K08-03 排查時需要清潔什麼", COOL, 1, "清潔散熱片"),
    hit("tw-ops", "W12-30 拔除多餘負載後等待多久才能啟動", PWR, 1, "十分鐘後重新啟動"),
    hit("cn-ops", "C21-05 更新證書以後控制台應輸入什麼", CAL, 1, "在控制台输入证书编号"),
    hit("hr", "带薪年假的天数和入职年限要求是什么", LEAVE, 1, "入职满一年享有 15 天带薪年假"),
    hit("hr", "调薪评审结果能被哪些人员看到", COMP, 1, "评审结果仅 HR 与直属主管可见"),
    hit("fin", "差旅行程结束后，多少天内必须提交报销", EXP, 1, "行程结束后 30 日内提交报销"),
    hit("fin", "哪个月份需要提交下一年度预算", BUD, 1, "每年 11 月提交下一年度预算"),
    hit("sg-ops", "首次登录公司账号时必须修改什么", ONB, 1, "首次登录须修改初始密码"),
    hit("hr", "VPN 客户端的下载位置是哪里", VPN, 1, "VPN 客户端可在 IT 门户下载"),
    hit("tw-ops", "冷卻液更換之前管路要怎麼處理", COOL, 1, "更換時須排空管路", "cross"),
    hit("tw-ops", "s5–19 校正零點之前應檢查哪個接頭", SEN, 2, "檢查感測器接頭是否鬆動", "cross"),
    hit("sg-ops", "电源保险丝更换时允许使用什么规格", PWR, 1, "15A 慢熔型", "cross"),
    hit("cn-ops", "中國區年度校準的證書應存檔在哪裡", CAL, 1, "CN 区 Ops 共享目录", "cross"),
    no("sg-ops", "E17-48 复位后应该观察多长时间"),
    no("sg-ops", "P03-16 需要打开进水阀吗"),
    no("tw-ops", "SNS-5F 異常要更換感測器嗎"),
    no("tw-ops", "K08-09 的散熱片應如何清潔"),
    no("tw-ops", "W12-36 等待十分鐘後可以啟動嗎"),
    no("cn-ops", "C21-09 的证书编号怎么输入"),
    access("hr", "E17-07 进料卡滞清理流程", E17),
    access("fin", "SEAL-9X 密封圈更换步骤", PUMP),
    access("sg-ops", "C21-05 清除错误的证书登记", CAL),
    access("tw-ops", "C21-05 證書編號登記方式", CAL),
]

if __name__ == "__main__":
    rows = [dict(id=f"v5-{c['category']}-{i + 1:02d}", split="val5", **c) for i, c in enumerate(cases)]
    Path(__file__).with_name("cases_v5.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in rows) + "\n", encoding="utf-8")
    print(len(rows), "cases")
