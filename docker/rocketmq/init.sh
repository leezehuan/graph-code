#!/bin/sh
set -eu
admin() {
    # mqadmin may print a Java exception yet exit zero. Require its success marker.
    output=$(sh mqadmin "$@" -n rocketmq-namesrv:9876 -b rocketmq-broker:10911 2>&1)
    echo "$output"
    echo "$output" | grep -q 'create .* success' || exit 1
}
for topic in agent-command task-event permission-event agent-message; do
    admin updateTopic -t "$topic" -a '+message.type=NORMAL'
done
# Separate NORMAL groups from any existing Lite group attributes/offsets.
group() {
    admin updateSubGroup -g "$1-normal"
}
group GID-agent-runtime-pool
group GID-langcode-smoke
for name in permissions messages task-events control; do
    group "GID-langcode-lead-$name"
done
for runtime in $(echo "${LANGCODE_RUNTIME_IDS:-runtime-001,runtime-002,runtime-003}" | tr ',' ' '); do
    for name in control permission inbox; do
        group "GID-agent-runtime-$name-$runtime"
    done
done
