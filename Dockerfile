FROM ubuntu:26.04
LABEL maintainer="Aerya <blog@upandclear.org>"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc \
    | gpg --dearmor -o /usr/share/keyrings/tor-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/tor-archive-keyring.gpg] https://deb.torproject.org/torproject.org jammy main" \
    > /etc/apt/sources.list.d/tor.list

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        tor \
        deb.torproject.org-keyring \
        privoxy \
        haproxy \
        python3 \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Pebble is inherited from Ubuntu but is not used by ProxySpin.
# Removing it eliminates an unnecessary Go binary and its embedded dependencies.
RUN rm -f /usr/bin/pebble

COPY start.py /usr/local/bin/start.py
COPY cfworker.py /usr/local/bin/cfworker.py
COPY cloudflare_worker.js /usr/local/bin/cloudflare_worker.js
COPY scripts/container-healthcheck.py /usr/local/bin/proxyspin-healthcheck
RUN chmod +x /usr/local/bin/start.py /usr/local/bin/proxyspin-healthcheck

EXPOSE 1973 1974 1976

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python3", "/usr/local/bin/proxyspin-healthcheck"]

CMD ["python3", "/usr/local/bin/start.py"]
