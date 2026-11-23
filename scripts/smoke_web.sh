#!/usr/bin/env bash
# 容器/本地冒烟：启动 Web 后跑一遍跳题判定、冲突补问与试跑核查。
# 依赖 curl + 可用的 Flask 环境（容器内已具备）。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
jar="$(mktemp -t cookies.XXXXXX)"
trap 'rm -f "$jar"' EXIT

# POST 不跟随重定向，保留 flash 给下一步 GET 读取；返回 3xx 即成功。
post() { # url  "k=v&k=v"
  local url="$1" pairs="$2"
  local args=() pair
  local IFS='&'
  for pair in $pairs; do args+=(--data-urlencode "$pair"); done
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" \
         "${args[@]}" "$BASE$url")
  case "$code" in
    2??|3??) echo "$code" ;;
    *) echo "HTTP $code" >&2; exit 1 ;;
  esac
}

get() { curl -sS -b "$jar" -c "$jar" "$BASE$1"; }

expect() { # 页面 文本 说明
  echo "$1" | grep -q "$2" && echo "   $3 OK" || { echo "   FAIL: 未找到“$2”"; exit 1; }
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
echo "   $(post /setup "config_text=$payload")"

page="$(get /)"
expect "$page" "结论有分歧" "分歧状态"
expect "$page" "从当前起最坏还要问几题" "最坏题数卡片"

echo "2) 冲突补问：S2 被跳题约束隐含为假，答是应被拒绝并保留原结果"
echo "   $(post /answer "item=S2&value=true")"
page="$(get /)"
expect "$page" "与旧档记录冲突" "冲突提示"
expect "$page" "上次提交因冲突被拒绝" "保留提交前结果"

echo "3) 正常补问 S3=是，应直接判定纳入"
echo "   $(post /answer "item=S3&value=true")"
page="$(get /)"
expect "$page" "纳入新版夜骑分析" "纳入判定"

echo "4) 试跑核查：载入 v2.3 版号配置"
versions='{"versions":[{"version":"v2.3","items":["S1","S2","S3"],
"followups":[{"qid":"Q1","condition":"S1 AND NOT S2"},{"qid":"Q2","condition":"S3 OR S1"}]}]}'
echo "   $(post /trials/versions "versions_text=$versions")"

echo "5) 上传两条不同终端、一跳一显的试跑记录，应报异常"
trials='{"trial_id":"R1","terminal":"T-A","version":"v2.3","answers":{"S1":true,"S2":false,"S3":true},"followups":[{"qid":"Q1","shown":true},{"qid":"Q2","shown":true}]}
{"trial_id":"R2","terminal":"T-B","version":"v2.3","answers":{"S1":true,"S2":false,"S3":true},"followups":[{"qid":"Q1","shown":false},{"qid":"Q2","shown":true}]}'
echo "   $(post /trials/records "trials_text=$trials")"
page="$(get /trials)"
expect "$page" "T-A" "异常组证据"
expect "$page" "不符预期" "预期不符标注"

echo "6) 相同内容重传不增计"
echo "   $(post /trials/records "trials_text=$trials")"
page="$(get /trials)"
expect "$page" "未增计 2 条" "去重"

echo "7) 按试跑号更正 R2 为展示，异常应撤下"
correct='{"trial_id":"R2","terminal":"T-B","version":"v2.3","answers":{"S1":true,"S2":false,"S3":true},"followups":[{"qid":"Q1","shown":true},{"qid":"Q2","shown":true}]}'
echo "   $(post /trials/records/R2/correct "record_json=$correct")"
page="$(get /trials)"
expect "$page" "没有异常组" "更正后异常撤下"

echo "全部冒烟通过。"
