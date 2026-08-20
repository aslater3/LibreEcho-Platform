# Build context must contain rootfs/ (from unpack-bootimg.py) and the entrypoints.
FROM scratch
COPY rootfs/ /
COPY entrypoint.sh /entrypoint.sh
COPY entrypoint-mock.sh /entrypoint-mock.sh
EXPOSE 8080
ENTRYPOINT ["/bin/busybox","sh","/entrypoint.sh"]
