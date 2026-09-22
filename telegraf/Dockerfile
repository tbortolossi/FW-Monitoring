FROM debian:bookworm-slim AS mib-downloader

# Fetch the standard IANA/IETF MIB corpus in a build-only stage. Alpine ships
# the common IF/HOST-RESOURCES MIBs but not the ENTITY MIBs used by chassis
# monitoring. No Debian package or library is copied into the runtime image.
RUN echo "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] \
     http://deb.debian.org/debian bookworm contrib non-free non-free-firmware" \
     > /etc/apt/sources.list.d/nonfree.list && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates snmp-mibs-downloader && \
    download-mibs && \
    rm -f /var/lib/mibs/ietf/SNMPv2-PDU && \
    rm -rf /var/lib/apt/lists/*

FROM telegraf:1.40.1-alpine

USER root

RUN apk add --no-cache python3 net-snmp-tools && \
    # Keep the UID used by the previous Debian runtime so existing bind-mounted
    # logs remain writable during an in-place upgrade.
    sed -i 's/^telegraf:x:100:101:/telegraf:x:999:101:/' /etc/passwd && \
    grep -q '^telegraf:x:999:101:' /etc/passwd && \
    chown -R 999:101 /etc/telegraf /home/telegraf

COPY --from=mib-downloader /var/lib/mibs/ /var/lib/mibs/
RUN ln -s /var/lib/mibs/iana /usr/share/snmp/mibs/iana && \
    ln -s /var/lib/mibs/ietf /usr/share/snmp/mibs/ietf

# Vendor MIBs (Palo Alto + Fortinet).
RUN mkdir -p /usr/share/snmp/mibs/vendors/paloalto \
             /usr/share/snmp/mibs/vendors/fortinet
COPY mibs/ /usr/share/snmp/mibs/vendors/

# Generated Telegraf config.
#COPY telegraf.conf /etc/telegraf/telegraf.conf

# Always load all MIBs.

# Net-SNMP paths.
ENV MIBDIRS=/usr/share/snmp/mibs:\
/usr/share/snmp/mibs/ietf:\
/usr/share/snmp/mibs/iana:\
/usr/share/snmp/mibs/vendors:\
/usr/share/snmp/mibs/vendors/paloalto:\
/usr/share/snmp/mibs/vendors/fortinet \
MIBS=+ALL

# Read-only PAN-OS XML API performance collector used by inputs.execd.
COPY paloalto_api_collector.py /usr/local/bin/paloalto_api_collector.py

# Make the runtime privilege boundary explicit instead of relying only on the
# upstream entrypoint to drop from root to the Telegraf account.
USER telegraf
