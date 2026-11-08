FROM python:3.12-slim
WORKDIR /task
COPY task.py /task/task.py
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python", "-IB", "/task/task.py"]
