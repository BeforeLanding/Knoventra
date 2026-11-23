"""Generate the fictional field-engineer technical journals used by Knoventra v4.

Everything here is invented: engineers, machines, customers, alarm codes, SOP
numbers, and the "sensitive" strings (fake password and reserved-style phone
numbers that exist only to exercise redaction). No real company data is used.

The layout mirrors a common journal template: one sheet with the headers
仪器编号 / 日期 / 技术员 / 工作主题 / 工作内容 / 注意事项 / 公司名称. Daily journal
rows come first. Skill-matrix rows follow in the same sheet and reuse those
columns: no date, the skill item in 工作主题, and "作业次数：N；掌握程度：X" in 工作内容.

Output is deterministic for a given seed. Run from the repository root:
    uv run --project backend python data/demo/generate_journals.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

HEADERS = ["仪器编号", "日期", "技术员", "工作主题", "工作内容", "注意事项", "公司名称"]
SHEET = "工作周报"
SEED = 20261123
JOURNAL_ROWS = 200
START = dt.date(2026, 3, 2)
OUT_DIR = Path(__file__).resolve().parent / "journals"
FIXED_TIMESTAMP = dt.datetime(2026, 11, 23, 0, 0, 0)

ENGINEERS = [
    {"name": "Chen Lixing", "region": "CN", "lang": "zh-Hans"},
    {"name": "Zhou Lan", "region": "CN", "lang": "zh-Hans"},
    {"name": "Lin Boyu", "region": "TW", "lang": "zh-Hant"},
    {"name": "Huang Yongqing", "region": "TW", "lang": "zh-Hant"},
    {"name": "Tan Wei Ming", "region": "SG", "lang": "zh-Hans"},
    {"name": "Priya Nair", "region": "SG", "lang": "en"},
]

MACHINES = [
    {"serial": "610101", "model": "WX-38", "region": "CN", "company": "NRS"},
    {"serial": "610102", "model": "WX-P", "region": "CN", "company": "NRS"},
    {"serial": "610103", "model": "ST-250", "region": "CN", "company": "HBF"},
    {"serial": "620201", "model": "WX-38", "region": "TW", "company": "LMC"},
    {"serial": "620202", "model": "WX-P", "region": "TW", "company": "LMC"},
    {"serial": "630301", "model": "WX-38", "region": "SG", "company": "SFC"},
    {"serial": "630302", "model": "ST-250", "region": "SG", "company": "SFC"},
]

ALL_MODELS = ("WX-38", "WX-P", "ST-250")


@dataclass(frozen=True)
class Text:
    topic: str
    content: str
    notes: str


@dataclass(frozen=True)
class Scenario:
    key: str
    skill: str | None
    models: tuple[str, ...]
    zh: Text
    en: Text | None = None
    typo_topic: str | None = None
    by_model: dict[str, Text] = field(default_factory=dict)
    resolved: bool = True
    sensitive: bool = False
    weight: int = 3


SCENARIOS = [
    Scenario(
        key="mixtank_alarm", skill="mix tank trouble shooting", models=("WX-38", "WX-P"), weight=5,
        zh=Text("mix tank 切换 tank 时报 alarm A-1301，pump 不转 trouble shooting",
                "1.确认 alarm A-1301 为 tank level sensor LS-2 信号丢失；2.检查发现 LS-2 接头松动，重新插紧；"
                "3.按 SOP-WCL-380110-001 第 4 节 reset interlock 后 pump 恢复运转",
                "1.reset interlock 前先确认 tank 内无残液；2.LS-2 接头易被管路拉扯，复装时加扎带固定"),
        en=Text("Mix tank alarm A-1301 when switching tank, pump not running",
                "1. A-1301 means level sensor LS-2 signal lost; 2. LS-2 connector found loose and reseated; "
                "3. Reset interlock per SOP-WCL-380110-001 section 4, pump recovered",
                "1. Confirm the tank is empty before interlock reset; 2. Secure the LS-2 cable with a tie wrap"),
        typo_topic="mix tank 切换报 A-1301 trouble shotting",
    ),
    Scenario(
        key="spindle_replace", skill="spindle replacement", models=("WX-38", "WX-P"), weight=4,
        zh=Text("spindle 更换", "", ""),
        by_model={
            "WX-38": Text("spindle 更换（单面机）",
                          "1.拆除旧 spindle 前确认 chuck 已回原点；2.新 spindle 固定螺丝按对角顺序锁付，扭矩 12 N·m；"
                          "3.完成后执行 run-out 检查，要求 < 5 μm",
                          "单面机只有一组 spindle，扭矩不要按双面机设置"),
            "WX-P": Text("spindle 更换（双面机）",
                         "1.拆除前确认上下 chuck 均已回原点；2.上下两组 spindle 分别按对角顺序锁付，扭矩 18 N·m；"
                         "3.完成后上下分别执行 run-out 检查，要求 < 8 μm",
                         "双面机需先更换下组再更换上组，避免碰撞上 chuck"),
        },
    ),
    Scenario(
        key="chamber_teaching", skill="chamber teaching", models=ALL_MODELS, weight=4,
        zh=Text("chamber teaching",
                "1.ECO to chuck 位置示教；2.robot to load port 三点示教；3.teaching 完成后 dynamic test 跑 25 片 dummy wafer",
                "1.示教时 robot 速度降到 10%；2.各点位坐标备份到 config 文件"),
        en=Text("Chamber teaching",
                "1. ECO to chuck position teaching; 2. Robot to load port three-point teaching; "
                "3. Dynamic test with 25 dummy wafers after teaching",
                "1. Reduce robot speed to 10% while teaching; 2. Back up all coordinates to the config file"),
        typo_topic="chamber tecahing",
    ),
    Scenario(
        key="chemical_flow", skill="chemical flow 调试", models=ALL_MODELS,
        zh=Text("chemical flow 调试",
                "1.按 Chemical Flow Plan 核对管路；2.逐路开启 valve 确认 flow 读值；"
                "3.DHF 流量设定 1.2 L/min，偏差超过 5% 时调整 needle valve",
                "1.开阀前确认排风正常；2.调试期间穿戴耐酸手套和面罩"),
        typo_topic="chamical flow 调试",
    ),
    Scenario(
        key="di_flow", skill="DI flow 调试", models=ALL_MODELS,
        zh=Text("通水后 DI flow 调试",
                "1.先打开 DI 水阀确认管路有水；2.按 PM1 设定流量 2.0 L/min；3.flow 读值跳动时检查 flow meter 前端 filter 是否堵塞",
                "通水前再次确认所有接头已锁紧，通水后巡检 10 分钟无渗漏"),
        en=Text("DI flow tuning after water turn-on",
                "1. Open the DI valve and confirm water in the line; 2. Set flow to 2.0 L/min per PM1; "
                "3. If the reading fluctuates, check the filter upstream of the flow meter",
                "Recheck every fitting before turn-on and walk the line for 10 minutes afterwards"),
        typo_topic="DI flow trouble shotting",
    ),
    Scenario(
        key="golden_check", skill="golden check", models=("WX-38", "WX-P"),
        zh=Text("golden check",
                "1.teaching 后用 golden wafer 跑位置确认；2.检查 wafer 在 chuck 上偏心 < 0.3 mm；3.结果记录到 golden check 表",
                "golden wafer 用完放回专用 cassette，不可与 dummy wafer 混放"),
        en=Text("Golden check",
                "1. Run the golden wafer after teaching; 2. Wafer eccentricity on chuck must be < 0.3 mm; "
                "3. Record the result in the golden check sheet",
                "Return the golden wafer to its own cassette; never mix it with dummy wafers"),
    ),
    Scenario(
        key="loadport_level", skill="load port leveling", models=ALL_MODELS,
        zh=Text("load port leveling adjustment",
                "1.用水平仪确认 load port 四角水平；2.调整脚座至气泡居中；3.复测 robot 取放片无异响",
                "调整后锁紧脚座螺母并做标记，便于后续检查是否松动"),
    ),
    Scenario(
        key="emo_test", skill="EMO 测试", models=ALL_MODELS,
        zh=Text("EMO 测试",
                "1.逐个按下 EMO 确认机台断电；2.复位后检查 alarm 记录；3.填写 EMO 测试表",
                "测试前通知客户厂务，避免误判为异常断电"),
    ),
    Scenario(
        key="cda_tube", skill="CDA 管路验证", models=ALL_MODELS,
        zh=Text("CDA 供应管路连接验证",
                "1.检查 CDA 管件螺纹连接；2.螺纹处特氟龙带顺螺纹方向缠绕 2-3 层；3.保压 30 分钟，压降 < 0.02 MPa",
                "特氟龙带不要盖住管口第一牙，防止碎屑进入管路"),
    ),
    Scenario(
        key="door_override", skill="service door override", models=ALL_MODELS,
        zh=Text("service door override", "", ""),
        by_model={
            "ST-250": Text("service door override（ST-250）",
                           "1.先关闭 process module 遮光板；2.用钥匙开关切到 MAINT 位置解除 door interlock；"
                           "3.作业完成后切回 RUN 并在软件中清除 door alarm",
                           "ST-250 带遮光板，遮光板未关时 door override 无效"),
            "WX-38": Text("service door override（WX 系列）",
                          "1.在软件 Maintenance 页面打开 door override；2.确认运动部件已停止；3.作业完成后关闭 override 并清除 alarm",
                          "WX 系列无钥匙开关，override 只能在软件中开关"),
            "WX-P": Text("service door override（WX 系列）",
                         "1.在软件 Maintenance 页面打开 door override；2.确认上下运动部件均已停止；3.作业完成后关闭 override 并清除 alarm",
                         "WX 系列无钥匙开关，override 只能在软件中开关"),
        },
    ),
    Scenario(
        key="di_filter", skill="filter 更换", models=ALL_MODELS,
        zh=Text("install DI filter",
                "1.湿式 DI filter 安装前保持湿润；2.安装后先排气再通水；3.记录 filter 更换日期和批号",
                "filter 方向箭头与水流方向一致，装反会导致压差异常"),
    ),
    Scenario(
        key="nozzle_leak", skill=None, models=("WX-P",), resolved=False, weight=2,
        zh=Text("nozzle 间歇漏液，alarm A-2207 偶发",
                "1.更换 nozzle O-ring 后仍偶发 A-2207；2.怀疑 suck-back valve 动作延迟；3.已反馈原厂，待跟踪",
                "复现条件不明，先记录每次报警时的 recipe 和步骤"),
    ),
    Scenario(
        key="wafer_slip", skill=None, models=("ST-250",), resolved=False, weight=2,
        zh=Text("chuck 高速段 wafer 偏移报警 A-3105",
                "1.清洁 chuck pin 后仍复现；2.降低 ramp 速率后仍偶发；3.待原厂回复 firmware 修正，待跟踪",
                "临时措施：该 recipe 高速段转速降低 10%"),
    ),
    Scenario(
        key="pump_noise", skill=None, models=ALL_MODELS, resolved=False, weight=2,
        zh=Text("circulation pump 异响",
                "1.拆检发现 bearing 有磨损痕迹；2.备件未到，暂时降低 pump 转速运行；3.待跟踪，下周更换 bearing 后复测",
                "降速期间注意 tank 温度是否上升"),
    ),
    Scenario(
        key="sw_update", skill="软件安装及通讯测试", models=ALL_MODELS, sensitive=True, weight=2,
        zh=Text("软件更新及 EAP 通讯测试",
                "1.进入工程模式（口令：Fake-Eng-0420）更新 recipe；2.EAP 通讯测试正常；3.如需远程支持联系值班 FSE 电话 +65 9000 0001",
                "工程模式用完立即退出，避免客户误操作"),
    ),
]

# Each entry is (topic, content, notes) for a near-duplicate rewrite of an earlier row.
NEAR_DUP_SUFFIX = "（同上周，继续跟进）"


def _to_traditional(text: str) -> str:
    from opencc import OpenCC

    return OpenCC("s2tw").convert(text)


def _workdays(start: dt.date, count: int) -> list[dt.date]:
    days, d = [], start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def _text_for(scn: Scenario, model: str, lang: str, use_typo: bool) -> Text:
    if scn.by_model:
        base = scn.by_model[model]
    elif lang == "en" and scn.en is not None:
        base = scn.en
    else:
        base = scn.zh
    topic = scn.typo_topic if use_typo and scn.typo_topic else base.topic
    text = Text(topic, base.content, base.notes)
    if lang == "zh-Hant":
        text = Text(*(_to_traditional(v) for v in (text.topic, text.content, text.notes)))
    return text


def build(seed: int = SEED) -> dict:
    rng = random.Random(seed)
    days = _workdays(START, JOURNAL_ROWS)
    rows: list[dict] = []
    for i in range(JOURNAL_ROWS):
        eng = ENGINEERS[i % len(ENGINEERS)]
        machines = [m for m in MACHINES if m["region"] == eng["region"]]
        candidates = [s for s in SCENARIOS
                      if any(m["model"] in s.models for m in machines)
                      and (eng["lang"] != "en" or s.en is not None or s.by_model or not s.resolved)]
        scn = rng.choices(candidates, weights=[s.weight for s in candidates])[0]
        machine = rng.choice([m for m in machines if m["model"] in scn.models])
        use_typo = scn.typo_topic is not None and rng.random() < 0.35
        lang = eng["lang"] if not (eng["lang"] == "en" and scn.en is None) else "zh-Hans"
        text = _text_for(scn, machine["model"], lang, use_typo)
        labels = []
        if use_typo:
            labels.append("typo")
        if scn.by_model:
            labels.append("model_variant")
        if not scn.resolved:
            labels.append("unresolved")
        if scn.sensitive:
            labels.append("sensitive")
        rows.append({
            "serial": machine["serial"], "date": days[i], "engineer": eng["name"],
            "topic": text.topic, "content": text.content, "notes": text.notes,
            "company": machine["company"], "region": eng["region"], "model": machine["model"],
            "scenario": scn.key, "skill": scn.skill, "lang": lang, "labels": labels,
        })

    # Weekly-report copy-paste: re-emit earlier rows a week later by the same engineer.
    for src in rng.sample(range(0, JOURNAL_ROWS - 10), 6):
        orig = rows[src]
        exact = len([r for r in rows if "exact_duplicate" in r["labels"]]) < 3
        dup = dict(orig, date=orig["date"] + dt.timedelta(days=7), labels=list(orig["labels"]))
        if exact:
            dup["labels"].append("exact_duplicate")
        else:
            dup["content"] = orig["content"] + NEAR_DUP_SUFFIX
            dup["labels"].append("near_duplicate")
        dup["duplicate_of"] = src
        rows.append(dup)
    rows.sort(key=lambda r: (r["region"], r["date"], r["engineer"]))
    return {"seed": seed, "journal_rows": rows, "skill_rows": _skill_rows(rows)}


def _level(count: int) -> str:
    return "不了解" if count == 0 else "了解" if count < 3 else "掌握"


def _skill_rows(rows: list[dict]) -> list[dict]:
    out = []
    skills = sorted({s.skill for s in SCENARIOS if s.skill})
    for m in MACHINES:
        for eng in (e for e in ENGINEERS if e["region"] == m["region"]):
            for skill in skills:
                scn = next(s for s in SCENARIOS if s.skill == skill)
                if m["model"] not in scn.models:
                    continue
                count = sum(1 for r in rows if r["engineer"] == eng["name"] and r["serial"] == m["serial"]
                            and r["skill"] == skill and "exact_duplicate" not in r["labels"]
                            and "near_duplicate" not in r["labels"])
                out.append({
                    "serial": m["serial"], "engineer": eng["name"], "skill": skill, "count": count,
                    "level": _level(count), "note": "未实际操作，仅旁观" if count == 0 and skill == "golden check" else None,
                    "company": m["company"], "region": m["region"],
                })
    return out


def write(data: dict, out_dir: Path = OUT_DIR) -> dict:
    from openpyxl import Workbook

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows, files = [], []
    for region in ("CN", "TW", "SG"):
        wb = Workbook()
        wb.properties.creator = "knoventra demo generator"
        wb.properties.created = FIXED_TIMESTAMP
        wb.properties.modified = FIXED_TIMESTAMP
        ws = wb.active
        ws.title = SHEET
        ws.append(HEADERS)
        name = f"journals-{region}.xlsx"
        for r in (r for r in data["journal_rows"] if r["region"] == region):
            ws.append([r["serial"], dt.datetime.combine(r["date"], dt.time()), r["engineer"],
                       r["topic"], r["content"], r["notes"], r["company"]])
            entry = {"file": name, "row": ws.max_row, "scenario": r["scenario"], "model": r["model"],
                     "engineer": r["engineer"], "lang": r["lang"], "labels": r["labels"]}
            manifest_rows.append(entry)
        skill_count = 0
        for s in (s for s in data["skill_rows"] if s["region"] == region):
            ws.append([s["serial"], None, s["engineer"], s["skill"],
                       f"作业次数：{s['count']}；掌握程度：{s['level']}", s["note"], s["company"]])
            skill_count += 1
        wb.save(out_dir / name)
        files.append({"file": name, "region": region, "skill_rows": skill_count})

    labels: dict[str, int] = {}
    for r in manifest_rows:
        for label in r["labels"]:
            labels[label] = labels.get(label, 0) + 1
    manifest = {
        "generator": "data/demo/generate_journals.py",
        "seed": data["seed"],
        "content_sha256": fingerprint(data),
        "headers": HEADERS,
        "engineers": ENGINEERS,
        "machines": MACHINES,
        "files": files,
        "journal_rows": len(manifest_rows),
        "skill_rows": len(data["skill_rows"]),
        "label_counts": dict(sorted(labels.items())),
        "rows": manifest_rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                                           encoding="utf-8")
    return manifest


def fingerprint(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def check(manifest: dict) -> None:
    """Every injected difficulty must appear at least three times."""
    required = ("typo", "near_duplicate", "exact_duplicate", "unresolved", "model_variant", "sensitive")
    missing = [k for k in required if manifest["label_counts"].get(k, 0) < 3]
    if missing:
        raise SystemExit(f"fewer than 3 rows labelled: {', '.join(missing)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    manifest = write(build(args.seed), args.out)
    check(manifest)
    print(f"{manifest['journal_rows']} journal rows, {manifest['skill_rows']} skill rows -> {args.out}")
    print(json.dumps(manifest["label_counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
