# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
# Non-root, read-only rootfs friendly (nothing is written at runtime).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin pim
COPY --from=build /install /usr/local
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["uvicorn", "pim_tools.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
