# -*- coding: utf-8 -*-
"""鸿蒙机会雷达（Harmony Opportunity Radar）主包。

流水线分三阶段（对应 CLI 子命令）:
  capture  Apple 标杆捕获: 拉榜单 → LLM 精选 → JTBD 抽象 → data/benchmarks.json
  probe    鸿蒙供给探测: 检索 AppGallery → 详情/评论抓取（M2）
  audit    四维打分审计 + 决策看板（M3）
"""
