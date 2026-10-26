#!/bin/bash
# garmin-connector v1 -- started by the Lambda Web Adapter (AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap)
PATH=$PATH:$LAMBDA_TASK_ROOT/bin:/opt/bin \
  PYTHONPATH=$LAMBDA_TASK_ROOT:/opt/python:$LAMBDA_RUNTIME_DIR \
  exec python -m uvicorn --port="$PORT" --no-access-log app:app
