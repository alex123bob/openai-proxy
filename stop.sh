#!/bin/sh
cd "$(dirname "$0")"
[ -f proxy.pid ] && kill "$(cat proxy.pid)" && rm proxy.pid && echo stopped
