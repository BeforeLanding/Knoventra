# Elasticsearch with the IK Chinese analyzer, pinned by digest and checksum.
# Fallback if IK ever stops matching the ES version: install the official
# analysis-smartcn plugin instead and set KNV_ES_ANALYZER=smartcn.
FROM docker.elastic.co/elasticsearch/elasticsearch:8.15.3@sha256:01c1732062b4a846c5ca687b0094b89bad0bfed00c6d71626db32fb8f3131a78

ARG IK_URL=https://get.infini.cloud/elasticsearch/analysis-ik/8.15.3
ARG IK_SHA256=93d5fe19901e6002ba112f4eaf223b608f07550d7e084f3cd8274f43baa5e466

RUN curl -fsSL -o /tmp/ik.zip "$IK_URL" \
 && echo "$IK_SHA256  /tmp/ik.zip" | sha256sum -c - \
 && bin/elasticsearch-plugin install --batch file:///tmp/ik.zip \
 && rm /tmp/ik.zip
