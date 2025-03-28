FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.9

COPY app.py ./
COPY requirements.txt  .
RUN  pip3 install -r requirements.txt --target "${LAMBDA_TASK_ROOT}"
COPY app.py ${LAMBDA_TASK_ROOT}

# You can overwrite command in `serverless.yml` template
CMD ["app.handler"]