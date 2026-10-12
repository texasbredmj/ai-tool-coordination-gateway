FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
COPY examples ./examples
COPY scripts ./scripts
RUN pip install --no-cache-dir -r requirements.lock . && useradd --create-home gateway \
    && mkdir /data && chown gateway:gateway /data
USER gateway
ENTRYPOINT ["tool-gateway"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
