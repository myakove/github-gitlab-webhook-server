FROM quay.io/podman/stable:latest

EXPOSE 5000

ENV USERNAME="podman"
ENV HOME_DIR="/home/$USERNAME"
ENV BIN_DIR="$HOME_DIR/.local/bin"
ENV PATH="$PATH:$BIN_DIR"
ENV DATA_DIR="$HOME_DIR/data"
ENV APP_DIR="$HOME_DIR/github-webhook-server"

RUN dnf -y install dnf-plugins-core \
  && dnf -y update \
  && dnf -y install \
  git \
  hub \
  unzip \
  gcc \
  python3-devel \
  && dnf clean all \
  && rm -rf /var/cache /var/log/dnf* /var/log/yum.*


RUN mkdir -p $BIN_DIR \
  && mkdir -p $APP_DIR \
  && mkdir -p $DATA_DIR \
  && mkdir -p $DATA_DIR/logs

COPY entrypoint.sh pyproject.toml uv.lock README.md $APP_DIR/
COPY webhook_server_container $APP_DIR/webhook_server_container/

RUN usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USERNAME \
  && chown -R $USERNAME:$USERNAME $HOME_DIR

USER $USERNAME
WORKDIR $HOME_DIR

RUN python3 -m pip install uv

RUN set -x \
  && curl https://mirror.openshift.com/pub/openshift-v4/clients/rosa/latest/rosa-linux.tar.gz --output $BIN_DIR/rosa-linux.tar.gz \
  && tar xvf $BIN_DIR/rosa-linux.tar.gz \
  && mv rosa $BIN_DIR/rosa \
  && chmod +x $BIN_DIR/rosa \
  && rm -rf $BIN_DIR/rosa-linux.tar.gz

WORKDIR $APP_DIR

RUN uv sync

HEALTHCHECK CMD curl --fail http://127.0.0.1:5000/webhook_server/healthcheck || exit 1

ENTRYPOINT ["./entrypoint.sh"]
