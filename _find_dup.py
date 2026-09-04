# -*- coding: utf-8 -*-
"""临时脚本：找出 triples.db 中 神经层/LLM 重复的事实（用后即删）。"""
import sqlite3

conn = sqlite3.connect(r"e:\研一\暑期作业\知识图谱构建项目\MedGraph\data\triples.db")
rows = conn.execute("SELECT id, subject, relation, object, confidence, layer FROM triples ORDER BY subject, object").fetchall()

print("== 全部 18 条 ==")
for r in rows:
    print(f"  #{r[0]:>2} {r[1]} --{r[2]}--> {r[3]}  conf={r[4]}  [{r[5]}]")

print("\n== 按 (subject, object) 分组，超过 1 层的视为跨层重复 ==")
from collections import defaultdict
groups = defaultdict(list)
for r in rows:
    groups[(r[1], r[3])].append(r)
for (s, o), items in sorted(groups.items()):
    layers = {i[5] for i in items}
    if len(items) > 1:
        print(f"  {s} --*--> {o}: {len(items)} 条, 层={layers}")
        for i in items:
            print(f"      #{i[0]} {s} --{i[2]}--> {o} conf={i[4]} [{i[5]}]")
conn.close()
