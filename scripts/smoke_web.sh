#!/usr/bin/env bash
# 容器/本地冒烟：启动 Web 后跑一遍跳题判定与冲突补问。
# 依赖 curl + 可用的 Flask 环境（容器内已具备）。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
jar="$(mktemp -t cookies.XXXXXX)"
trap 'rm -f "$jar"' EXIT

post() { # url  data
  curl -sS -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" -L \
       --data-urlencode "$2" "$BASE$1"
}

echo "1) 建立任务（配置+相容答卷）"
payload="$(cat <<'JSON'
{"config":{"items":["S1","S2","S3"],"labels":{"S1":"头盔","S2":"车灯","S3":"超30分钟"},
"followups":[{"qid":"Q1","condition":"S1 AND NOT S2","shown":true},
{"qid":"Q2","condition":"S3 OR S1","shown":true}],
"inclusion":"S1 AND (S2 OR S3)"},
"archive":{"answers":{"S1":true},
"followups":[{"qid":"Q1","shown":true},{"qid":"Q2","shown":true}]}}
JSON
)"
code=$(post /setup "config_text=$payload")
[ "$code" = "200" ] && echo "   OK ($code)" || { echo "   FAIL $code"; exit 1; }

page="$(curl -sS -b "$jar" "$BASE/")"
echo "$page" | grep -q "结论有分歧" && echo "2) 分歧状态与最坏题数展示 OK" || { echo "2) FAIL"; exit 1; }
echo "$page" | grep -q "从当前起最坏还要问几题" && echo "   最坏题数卡片 OK"

echo "3) 冲突补问：S2 被跳题约束隐含为假，答是应被拒绝并保留原结果"
code=$(post /answer "item=S2&value=true")
page="$(curl -sS -b "$jar" "$BASE/")"
echo "$page" | grep -q "与旧档记录冲突" && echo "   冲突提示 OK" || { echo "   FAIL"; exit 1; }
echo "$page" | grep -q "上次提交因冲突被拒绝" && echo "   保留提交前结果 OK"

echo "4) 正常补问 S3=是，应直接判定纳入"
code=$(post /answer "item=S3&value=true")
page="$(curl -sS -b "$jar" "$BASE/")"
echo "$page" | grep -q "纳入新版夜骑分析" && echo "   纳入判定 OK" || { echo "   FAIL"; exit 1; }

echo "全部冒烟通过。"
