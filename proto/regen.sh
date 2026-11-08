#!/usr/bin/env bash
# Regenerate the *_pb2.py modules from DFHack's .proto files.
#
# The .proto files are copied from DFHack at the tag matching the installed
# DFHack (53.16-r3): library/proto/ and plugins/remotefortressreader/proto/.
# Generate with protoc 29 (grpcio-tools 1.71) so the output runs on any
# protobuf runtime from 5.29 up; a newer protoc emits code that refuses to
# load on older runtimes.
#
#   python3 -m venv /tmp/protoc-venv
#   /tmp/protoc-venv/bin/pip install grpcio-tools==1.71.0
#   PYTHON=/tmp/protoc-venv/bin/python ./regen.sh
set -euo pipefail
cd "$(dirname "$0")"
"${PYTHON:-python3}" -m grpc_tools.protoc -I. --python_out=. ./*.proto
