FROM telegraf:1.32

# 1) Enable non-free repositories for snmp-mibs-downloader.
RUN echo "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] \
     http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" \
     > /etc/apt/sources.list.d/nonfree.list && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        snmp snmp-mibs-downloader nano && \
    download-mibs && \
    # 2) Allow Net-SNMP to load all discovered MIBs.
    sed -i 's/^mibs *:/# &/' /etc/snmp/snmp.conf && \
    # 3) Remove the old SNMPv2-PDU file to avoid the "Bad operator" warning.
    rm -f /usr/share/snmp/mibs/ietf/SNMPv2-PDU && \
    # 4) Cleanup.
    rm -rf /var/lib/apt/lists/*

# 5) Vendor MIBs (Palo Alto + Fortinet).
RUN mkdir -p /usr/share/snmp/mibs/vendors/paloalto \
             /usr/share/snmp/mibs/vendors/fortinet
COPY mibs/ /usr/share/snmp/mibs/vendors/

# 6) Generated Telegraf config.
#COPY telegraf.conf /etc/telegraf/telegraf.conf

# 7) Always load all MIBs.

# Net-SNMP paths.
ENV MIBDIRS=/usr/share/snmp/mibs:\
/usr/share/snmp/mibs/ietf:\
/usr/share/snmp/mibs/iana:\
/usr/share/snmp/mibs/vendors:\
/usr/share/snmp/mibs/vendors/paloalto:\
/usr/share/snmp/mibs/vendors/fortinet \
MIBS=+ALL
