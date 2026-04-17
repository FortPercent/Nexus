#!/usr/bin/env bash
# 一键跑 adapter 所有回归测试。容器内执行：
#   docker exec teleai-adapter bash /app/scripts/run_all.sh
# 输出每个套件 pass/total + 总分。

set -u

cd "$(dirname "$0")/.." || exit 1

SUITES=(
  "regression.py"
  "todo_e2e.py"
  "ai_todo_e2e.py"
  "file_upload_e2e.py"
)

total_pass=0
total_all=0
results=()

for s in "${SUITES[@]}"; do
  echo "======== $s ========"
  out=$(python "scripts/$s" 2>&1)
  # 最后一行应类似 "==== 30/30 PASS ====" 或 "==== 18/18 ===="
  last=$(echo "$out" | tail -5 | grep -oE '====[^=]+====' | tail -1)
  # 解析 N/M
  nums=$(echo "$last" | grep -oE '[0-9]+/[0-9]+' | head -1)
  if [[ -n "$nums" ]]; then
    p=${nums%/*}
    n=${nums#*/}
    total_pass=$((total_pass + p))
    total_all=$((total_all + n))
    # FAIL 行 grep 一下
    fail_lines=$(echo "$out" | grep -E '\[FAIL\]|FAIL:' | head -3)
    if [[ -n "$fail_lines" ]]; then
      echo "$fail_lines"
      results+=("❌ $s: $p/$n")
    else
      results+=("✅ $s: $p/$n")
    fi
  else
    results+=("⚠️  $s: 没解析出结果")
    echo "$out" | tail -10
  fi
  echo ""
done

# smoke_stream 单独跑（只有 qwen 基线，letta 需要环境变量）
echo "======== smoke_stream.py (qwen-no-mem) ========"
sm_out=$(python scripts/smoke_stream.py 2>&1)
if echo "$sm_out" | grep -q "PASS"; then
  results+=("✅ smoke_stream.py: qwen 基线 PASS")
  total_pass=$((total_pass + 1))
  total_all=$((total_all + 1))
else
  results+=("❌ smoke_stream.py: qwen 基线 FAIL")
  total_all=$((total_all + 1))
fi

echo ""
echo "============================================"
echo "汇总"
echo "============================================"
for r in "${results[@]}"; do echo "  $r"; done
echo ""
if [[ "$total_pass" == "$total_all" ]]; then
  echo "🎉 全部通过: $total_pass/$total_all"
  exit 0
else
  echo "❌ 有失败: $total_pass/$total_all"
  exit 1
fi
