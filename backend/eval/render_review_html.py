"""Render a source-bound review packet as a local, offline human annotation form.

The browser exports the existing JSON packet format; run review_answers.py
summarize with the original report afterward. The page never auto-labels claims.
"""
from __future__ import annotations

import argparse
import base64
from html import escape
import json
from pathlib import Path

from answer_review import summarize_packet


VERDICTS = (
    ("unreviewed", "未审核"),
    ("supported", "完全支持"),
    ("partially_supported", "部分支持"),
    ("unsupported", "不支持"),
    ("not_a_claim", "非事实论断"),
)


def render(packet: dict, report: dict) -> str:
    # Bind the file to the original report before displaying editable controls.
    summarize_packet(packet, report)
    if any(claim["verdict"] != "unreviewed" for case in packet["cases"] for claim in case["claims"]):
        raise ValueError("render only an unreviewed packet; import a saved human draft in the browser")
    encoded = base64.b64encode(json.dumps(packet, ensure_ascii=False, allow_nan=False).encode("utf-8")).decode("ascii")
    sections = []
    for case_index, case in enumerate(packet["cases"]):
        snap = case["snapshot"]
        source_cards = []
        for source in snap["evidence"]:
            label = f'{source["alias"]} · {source["document_title"]} · 修订 {source["revision"]} · 第 {source["page"] or "-"} 页'
            cited = any(item["alias"] == source["alias"] for item in snap["sources"])
            source_cards.append(
                f'<details class="source"><summary>{escape(label)} · {"已展示引用" if cited else "当轮证据"}</summary>'
                f'<p class="section">{escape(source["section_path"])}</p><pre>{escape(source["text"])}</pre></details>'
            )
        claims = []
        for claim_index, claim in enumerate(case["claims"]):
            options = "".join(f'<option value="{key}">{label}</option>' for key, label in VERDICTS)
            citations = "、".join(claim["citations"]) or "无逐句引用"
            claims.append(
                f'<section class="claim" data-case="{case_index}" data-claim="{claim_index}">'
                f'<div class="claim-head"><strong>{escape(claim["id"])}</strong><span>{escape(citations)}</span></div>'
                f'<p class="claim-text">{escape(claim["text"])}</p>'
                f'<label>判定<select class="verdict">{options}</select></label>'
                f'<label>理由<textarea class="notes" rows="2" placeholder="说明哪些事实得到支持或缺少依据"></textarea></label>'
                f'<div class="supports"></div><button type="button" class="add-support">添加原文引句</button>'
                f'</section>'
            )
        sections.append(
            f'<article class="case"><h2>{escape(case["id"])} · {escape(snap["question"])}</h2>'
            f'<h3>模型回答</h3><pre class="answer">{escape(snap["answer"])}</pre>'
            f'<h3>当轮来源（可展开）</h3>{"".join(source_cards) or "<p>本轮无证据</p>"}'
            f'<h3>逐句审核</h3>{"".join(claims) or "<p>无审核单元</p>"}</article>'
        )
    return HTML.replace("__PACKET_BASE64__", encoded).replace("__CASES__", "\n".join(sections))


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Knoventra 回答支持度人工审核</title>
<style>
body{margin:0;background:#f4f7fb;color:#172337;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:2;background:#fff;border-bottom:1px solid #cbd5e1;padding:12px max(18px,calc((100vw - 1140px)/2));display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0 20px 0 0}h2{font-size:19px;margin:0 0 12px}h3{font-size:15px;margin:18px 0 8px}
main{max-width:1140px;margin:24px auto;padding:0 18px}.hint{background:#e8f1ff;border-left:4px solid #2773c9;padding:12px 16px;margin-bottom:20px}
.case{background:#fff;border:1px solid #d4deea;border-radius:10px;padding:22px;margin:22px 0;box-shadow:0 2px 9px #182c4610}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f9fc;border:1px solid #e2e8f0;border-radius:5px;padding:12px;margin:7px 0 12px;font:14px/1.65 system-ui,"Microsoft YaHei",sans-serif}
.answer{background:#fffdf2}.source{border:1px solid #d9e2ec;border-radius:5px;margin:7px 0}.source summary{cursor:pointer;padding:8px 12px}.source pre,.source .section{margin:8px 12px}.section{color:#516177}
.claim{border-top:1px solid #e1e7ef;padding:16px 0}.claim-head{display:flex;gap:12px;align-items:center}.claim-head span{color:#62748b;font-size:13px}
.claim-text{font-weight:600;margin:8px 0 12px;white-space:pre-wrap}.claim label{display:block;margin:8px 0;font-weight:600}.claim select,.claim textarea,.support-row select,.support-row input,header input{box-sizing:border-box;border:1px solid #aab9ca;border-radius:5px;padding:7px;background:#fff;color:#172337;font:inherit}
.claim label select,.claim label textarea{display:block;width:100%;font-weight:400}.claim select{max-width:240px}.support-row{display:flex;gap:7px;margin:7px 0}.support-row input{flex:1}.support-row button{flex:none}
button,.file-label{border:1px solid #3b6ea5;background:#fff;color:#144b84;border-radius:5px;padding:7px 11px;font:inherit;cursor:pointer}button:hover,.file-label:hover{background:#e8f1ff}.primary{background:#1768ba;color:#fff}.primary:hover{background:#125596}.file-label input{display:none}
@media(max-width:700px){header{position:static}.support-row{flex-wrap:wrap}.support-row input{min-width:100%}}
</style></head><body>
<header><h1>Knoventra 来源审核</h1><label>真人审核者 <input id="reviewer" placeholder="填写姓名或代号"></label><span id="progress"></span><button id="export" class="primary" type="button">导出 JSON</button><label class="file-label">导入进度<input id="import" type="file" accept=".json,application/json"></label></header>
<main><div class="hint">逐句阅读模型回答与当轮原文。完全 / 部分支持需填写逐字原文引句；不支持与非事实论断也需说明理由。可随时导出部分进度，之后导入继续。只有真人实际阅读后才能作为 human 审核；最终仍须用原报告运行 <code>review_answers.py summarize</code> 校验。此页面离线运行，不上传资料。</div>
__CASES__</main>
<script>
const packet=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob("__PACKET_BASE64__"),c=>c.charCodeAt(0))));
const editors=[...document.querySelectorAll('.claim')];let dirty=false;
function allowed(caseIndex,claimIndex){const c=packet.cases[caseIndex],cl=c.claims[claimIndex];return cl.citations.length?cl.citations:c.snapshot.evidence.map(e=>e.alias)}
function addSupport(editor,alias='',quote=''){const ci=Number(editor.dataset.case),cl=Number(editor.dataset.claim),choices=allowed(ci,cl);const row=document.createElement('div');row.className='support-row';const select=document.createElement('select');select.className='support-alias';for(const name of choices){const o=document.createElement('option');o.value=name;o.textContent=name;select.append(o)}if(alias)select.value=alias;const input=document.createElement('input');input.className='support-quote';input.placeholder='复制来源章节或正文中的连续原文';input.value=quote;const remove=document.createElement('button');remove.type='button';remove.textContent='移除';remove.onclick=()=>{row.remove();dirty=true};row.append(select,input,remove);editor.querySelector('.supports').append(row);dirty=true}
function updateProgress(){const n=editors.filter(e=>e.querySelector('.verdict').value!=='unreviewed').length;document.getElementById('progress').textContent=`已标注 ${n}/${editors.length}`}
for(const editor of editors){editor.querySelector('.add-support').onclick=()=>addSupport(editor);editor.querySelector('.verdict').onchange=()=>{dirty=true;updateProgress()};editor.querySelector('.notes').oninput=()=>{dirty=true}}
document.getElementById('reviewer').oninput=()=>{dirty=true};updateProgress();
function collect(){const name=document.getElementById('reviewer').value.trim();const output=JSON.parse(JSON.stringify(packet));for(const editor of editors){const ci=Number(editor.dataset.case),cl=Number(editor.dataset.claim),claim=output.cases[ci].claims[cl];claim.verdict=editor.querySelector('.verdict').value;if(claim.verdict==='unreviewed'){claim.reviewer=null;claim.reviewer_kind=null;claim.notes='';claim.supports=[];continue}if(!name)throw Error('请先填写真人审核者姓名或代号');claim.reviewer=name;claim.reviewer_kind='human';claim.notes=editor.querySelector('.notes').value.trim();if(!claim.notes)throw Error(`${output.cases[ci].id} ${claim.id} 缺少理由`);claim.supports=[];const evidence=output.cases[ci].snapshot.evidence;const displayed=new Set(output.cases[ci].snapshot.sources.map(s=>s.alias));for(const row of editor.querySelectorAll('.support-row')){const alias=row.querySelector('.support-alias').value,quote=row.querySelector('.support-quote').value.trim();if(!quote)continue;const source=evidence.find(s=>s.alias===alias);if(!source||(claim.citations.length&&(!claim.citations.includes(alias)||!displayed.has(alias)))||!(source.section_path+'\n'+source.text).includes(quote))throw Error(`${output.cases[ci].id} ${claim.id} 的引句不在允许的当轮来源中`);claim.supports.push({alias,quote})}if(['supported','partially_supported'].includes(claim.verdict)&&!claim.supports.length)throw Error(`${output.cases[ci].id} ${claim.id} 需要至少一条原文引句`)}return output}
document.getElementById('export').onclick=()=>{try{const data=collect(),blob=new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='knoventra-human-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);dirty=false}catch(error){alert(error.message)}};
document.getElementById('import').onchange=async event=>{
  const file=event.target.files[0];if(!file)return;
  try{
    const data=JSON.parse(await file.text());
    if(data.report_sha256!==packet.report_sha256||JSON.stringify(data.selected_case_ids)!==JSON.stringify(packet.selected_case_ids)||data.cases?.length!==packet.cases.length)throw Error('审核包与当前页面不匹配');
    // Validate the entire draft before changing any on-screen annotation.
    for(let ci=0;ci<packet.cases.length;ci++){
      const old=packet.cases[ci],incoming=data.cases[ci];
      if(incoming.id!==old.id||incoming.snapshot.sha256!==old.snapshot.sha256||incoming.claims?.length!==old.claims.length)throw Error('来源快照或用例已变化');
      for(let cl=0;cl<old.claims.length;cl++){
        const x=old.claims[cl],y=incoming.claims[cl];
        if(y.id!==x.id||y.text!==x.text||JSON.stringify(y.citations)!==JSON.stringify(x.citations)||!['unreviewed','supported','partially_supported','unsupported','not_a_claim'].includes(y.verdict)||!['human',null].includes(y.reviewer_kind))throw Error('论断或审核类型不匹配');
      }
    }
    let reviewer='';
    for(let ci=0;ci<packet.cases.length;ci++)for(let cl=0;cl<packet.cases[ci].claims.length;cl++){
      const y=data.cases[ci].claims[cl],editor=document.querySelector(`.claim[data-case="${ci}"][data-claim="${cl}"]`);
      editor.querySelector('.verdict').value=y.verdict;editor.querySelector('.notes').value=y.notes||'';
      editor.querySelector('.supports').replaceChildren();for(const s of y.supports||[])addSupport(editor,s.alias,s.quote);
      if(y.reviewer_kind==='human')reviewer=y.reviewer;
    }
    document.getElementById('reviewer').value=reviewer;dirty=false;updateProgress();
  }catch(error){alert(error.message)}event.target.value='';
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue=''}});
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() in {args.packet.resolve(), args.report.resolve()}:
        parser.error("HTML output must differ from its source files")
    try:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        report = json.loads(args.report.read_text(encoding="utf-8"))
        html = render(packet, report)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as output:
            output.write(html)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"review page error: {error}\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
