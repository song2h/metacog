FROM busybox:1.36
ARG VERSION
ENV VERSION=$VERSION
COPY server.sh /server.sh
ENTRYPOINT ["sh", "/server.sh"]
