#!/bin/bash

########## READ THIS FIRST ##########
### After you implement the task, you need to set the LOCAL_TASK_DIR to the local directory of the task.

export LOCAL_TASK_DIR="YOUR IMPLEMENTED TASK LOCAL DIR" # e.g. ./tasks/game/magic_24
export REMOTE_OUTPUT_DIR="output_test_pos"  # use output_test_pos if you uploaded test data via upload_test_data_to_remote.py; else output_test after running start()

# **UNCOMMENT THIS IF YOU ARE ASSIGNED WITH A LITELLM KEY**
# export OPENAI_API_BASE="https://litellm-991596698159.us-west1.run.app"

export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
export CUA_ENV_API_URL="http://YOUR_REMOTE_MACHINE_IP:5000"

###### ONLY CHANGE THE FOLLOWING CODE WHEN NECESSARY ######




# Auto-extract TASK_CATEGORY and TASK_NAME from LOCAL_TASK_DIR
if [[ $LOCAL_TASK_DIR =~ \./tasks/([^/]+)/([^/]+) ]]; then
    export TASK_CATEGORY="${BASH_REMATCH[1]}"
    export TASK_NAME="${BASH_REMATCH[2]}"
    echo "Auto-extracted TASK_CATEGORY: $TASK_CATEGORY"
    echo "Auto-extracted TASK_NAME: $TASK_NAME"
elif [[ $LOCAL_TASK_DIR =~ /tasks/([^/]+)/([^/]+) ]]; then
    export TASK_CATEGORY="${BASH_REMATCH[1]}"
    export TASK_NAME="${BASH_REMATCH[2]}"
    echo "Auto-extracted TASK_CATEGORY: $TASK_CATEGORY"
    echo "Auto-extracted TASK_NAME: $TASK_NAME"
else
    echo "Warning: Could not extract TASK_CATEGORY and TASK_NAME from LOCAL_TASK_DIR path"
    export TASK_CATEGORY="tasks"
    export TASK_NAME="unknown"
fi

# Configuration for Remote Windows Environment
export CUA_ENV_TYPE="windows"
export CUA_TELEMETRY_DISABLED="true"
export CUA_ENV_VNC_URL=""
export XDG_DATA_HOME="./trycua"
export EVALUATION_OUTPUT_DIR="./trycua/cua-bench/$TASK_NAME"

uv run python -m cua_bench.batch.solver $LOCAL_TASK_DIR \
    --eval \
    --agent agenthle-agent \
    --model openai/computer-use-preview \
    --max-steps 500 \
    --evaluate-only \
    --output-dir $EVALUATION_OUTPUT_DIR



