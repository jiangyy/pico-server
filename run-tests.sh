#!/bin/sh
# 全部测试：./run-tests.sh；单个文件：./run-tests.sh tests.test_replication
cd "$(dirname "$0")"
if [ "$#" -eq 0 ]; then
    set -- discover -s tests -t .
fi
PYTHONPATH=src exec python3 -m unittest "$@"
