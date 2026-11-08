# 本地浏览器流程录像

[knoventra-mock-flow-20261108-final.mp4](knoventra-mock-flow-20261108-final.mp4) 是 2026-11-08 从本地 Edge 无头浏览器逐帧捕获并编码的 23 秒无声演示（1250×706，5 fps）。录制时使用独立 PostgreSQL 演示库、12 份虚构资料、离线 Hash 编码器，以及 `backend/dev/mock_server.py` 的固定回答；没有调用真实 LLM。

画面依次展示 SG / Ops 的 E17-42 来源卡与原文预览、TW / Ops 的繁体提问命中简体资料、HR 越权拒答、Ops 无依据拒答、转交确认与本人记录、管理员发布 E17 修订 2，以及发布后新问题只显示修订 2 来源。录像只证明本地浏览器 UI / API 流程可操作，不能当作 BGE-M3 检索效果或真实回答支持度证据。真实检索和 LLM 结果分别见[评测报告](../evaluation-report.md)第 14、15 节。
