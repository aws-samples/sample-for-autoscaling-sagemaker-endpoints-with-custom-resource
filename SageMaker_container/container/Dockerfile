FROM --platform=linux/amd64 python:3.13-slim
RUN apt-get -y update && apt-get install -y --no-install-recommends \
         wget \
         python3-pip \
         python3-setuptools \
         nginx \
         ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip --no-cache-dir install flask gunicorn
ENV PYTHONUNBUFFERED=TRUE
ENV PYTHONDONTWRITEBYTECODE=TRUE
ENV PATH="/opt/program:${PATH}"

# Set up the program in the image
COPY code /opt/program
WORKDIR /opt/program
RUN chmod +x /opt/program/serve