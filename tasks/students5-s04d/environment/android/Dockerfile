# The phone for this task: the shared Android base image plus this task's seed files and setup.
ARG BASE_IMAGE=drizz-android-twins:api34
FROM ${BASE_IMAGE}
COPY bench /opt/drizz/bench
COPY phone_service.py /opt/drizz/phone_service.py
ENV TASK_FILE=docs/dyn5_hits/S04/tasks/s04d.yaml MAX_STEPS=400
