# Build context must contain rootfs/ (from unpack-bootimg.py) and the entrypoints.
FROM scratch
COPY rootfs/ /
COPY entrypoint.sh /entrypoint.sh
COPY entrypoint-mock.sh /entrypoint-mock.sh
# Optional: values captured from a real device, so the mock backend reports
# measured hardware rather than invented defaults. See entrypoint-mock.sh.
COPY device-profile.json /etc/libreecho/device-profile.json
EXPOSE 8080
ENTRYPOINT ["/bin/busybox","sh","/entrypoint.sh"]
