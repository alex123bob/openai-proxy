#!/bin/sh
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
nohup venv/bin/python app.py > proxy.log 2>&1 &
echo $! > proxy.pid
echo "started pid $(cat proxy.pid)"
