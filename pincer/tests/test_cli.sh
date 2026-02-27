#!/usr/bin/env bash
# Pincer CLI Test Script — Sprint 5
# Run: chmod +x tests/test_cli.sh && ./tests/test_cli.sh

set -e
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
PASS=0
FAIL=0

test_cmd() {
    local desc="$1"
    local cmd="$2"
    local expect_exit="${3:-0}"

    printf "  Testing: %-50s " "$desc"
    if eval "$cmd" > /dev/null 2>&1; then
        if [ "$expect_exit" = "0" ]; then
            printf "${GREEN}PASS${NC}\n"
            PASS=$((PASS+1))
        else
            printf "${RED}FAIL (expected failure)${NC}\n"
            FAIL=$((FAIL+1))
        fi
    else
        if [ "$expect_exit" != "0" ]; then
            printf "${GREEN}PASS (expected failure)${NC}\n"
            PASS=$((PASS+1))
        else
            printf "${RED}FAIL${NC}\n"
            FAIL=$((FAIL+1))
        fi
    fi
}

echo ""
echo "Pincer CLI Test Suite"
echo "========================"
echo ""

echo "--- Global ---"
test_cmd "pincer --help"                    "pincer --help"
test_cmd "pincer --version"                 "pincer --version"

echo ""
echo "--- Core ---"
test_cmd "pincer config"                    "pincer config"
test_cmd "pincer chat --help"               "pincer chat --help"
test_cmd "pincer run --help"                "pincer run --help"

echo ""
echo "--- Cost & Budget ---"
test_cmd "pincer cost"                      "pincer cost"
test_cmd "pincer cost --days 7"             "pincer cost --days 7"
test_cmd "pincer cost --by-model"           "pincer cost --by-model"

echo ""
echo "--- Security ---"
test_cmd "pincer doctor"                    "pincer doctor"
test_cmd "pincer doctor --json"             "pincer doctor --json"
test_cmd "pincer audit --help"              "pincer audit --help"

echo ""
echo "--- Skills ---"
test_cmd "pincer skills list"               "pincer skills list"
test_cmd "pincer skills scan ./skills/weather" "pincer skills scan ./skills/weather"

echo ""
echo "--- Memory ---"
test_cmd "pincer memory stats"              "pincer memory stats"

echo ""
echo "--- Schedule ---"
test_cmd "pincer schedule list"             "pincer schedule list"

echo ""
echo "========================"
echo "Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
echo ""

if [ $FAIL -gt 0 ]; then
    exit 1
fi
