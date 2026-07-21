ARG BASE_IMAGE=quay.io/sclorg/python-312-c9s:c9s@sha256:012a2fe921fbbf6a297c6ff4827ed73b5dd21e08ccc34e4015586f97b17985fc

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc

ARG UV_SYNC_EXTRA_ARGS=""

ARG MIMALLOC_VERSION=v3.4.1
ARG MIMALLOC_COMMIT=927e97f0df3225710fa724104bba29d3d9037e71

# ngspice built as a shared library (libngspice) for PySpice's in-process binding,
# which backs schematic SPICE simulation. ngspice is not packaged for EL9, so it is
# compiled from the upstream release here (like mimalloc) and the .so copied into
# the final image — no ngspice CLI, no EPEL.
ARG NGSPICE_VERSION=46
ARG NGSPICE_SHA256=a0d1699af1940b06649276dcd6ff5a566c8c0cad01b2f7b5e99dedbb4d64c19b
ARG JACKCESS_VERSION=5.1.4


###################################################################################################
# Build mimalloc                                                                                  #
###################################################################################################

FROM ${BASE_IMAGE} AS mimalloc

ARG MIMALLOC_VERSION
ARG MIMALLOC_COMMIT

USER 0

RUN dnf install -y --best --nodocs --setopt=install_weak_deps=False gcc gcc-c++ make cmake git

RUN git clone --filter=blob:none --no-checkout https://github.com/microsoft/mimalloc.git /opt/app-root/src/mimalloc && \
    git -C /opt/app-root/src/mimalloc checkout --detach ${MIMALLOC_COMMIT} && \
    test "$(git -C /opt/app-root/src/mimalloc describe --tags --exact-match)" = "${MIMALLOC_VERSION}"

WORKDIR /opt/app-root/src/mimalloc

RUN mkdir -p out/release

WORKDIR /opt/app-root/src/mimalloc/out/release
RUN cmake ../.. && make


###################################################################################################
# Build libngspice (shared) for PySpice                                                          #
###################################################################################################

FROM ${BASE_IMAGE} AS ngspice

ARG NGSPICE_VERSION
ARG NGSPICE_SHA256

USER 0

RUN dnf install -y --best --nodocs --setopt=install_weak_deps=False \
    gcc make bison flex tar gzip autoconf automake libtool

# Use the official release archive, which includes a generated ./configure.
# This avoids requiring a newer Autoconf than the pinned EL9 base provides.
ADD https://downloads.sourceforge.net/project/ngspice/ng-spice-rework/${NGSPICE_VERSION}/ngspice-${NGSPICE_VERSION}.tar.gz /tmp/ngspice.tar.gz

RUN echo "${NGSPICE_SHA256}  /tmp/ngspice.tar.gz" | sha256sum -c - && \
    mkdir -p /tmp/ngspice-src && \
    tar -xzf /tmp/ngspice.tar.gz -C /tmp/ngspice-src --strip-components=1

WORKDIR /tmp/ngspice-src
# --with-ngshared builds libngspice.so (the cffi target PySpice loads); no CLI,
# no X11/GUI. Installs into /usr/local/lib.
RUN ./configure --with-ngshared --disable-debug --without-x \
        --prefix=/usr/local CFLAGS="-O2" && \
    make -j"$(nproc)" && \
    make install


FROM ${BASE_IMAGE} AS jackcess

ARG JACKCESS_VERSION
USER 0
RUN dnf install -y --best --nodocs --setopt=install_weak_deps=False maven && \
    mvn -q -Dmaven.repo.local=/tmp/m2 dependency:get -Dartifact=io.github.spannm:jackcess:${JACKCESS_VERSION} && \
    mkdir -p /opt/jackcess && \
    cp /tmp/m2/io/github/spannm/jackcess/${JACKCESS_VERSION}/jackcess-${JACKCESS_VERSION}.jar /opt/jackcess/ && \
    cp /tmp/m2/org/apache/poi/poi/*/poi-*.jar /opt/jackcess/ && \
    cp /tmp/m2/commons-codec/commons-codec/*/commons-codec-*.jar /opt/jackcess/ && \
    cp /tmp/m2/org/apache/commons/commons-collections4/*/commons-collections4-*.jar /opt/jackcess/ && \
    cp /tmp/m2/org/apache/commons/commons-math3/*/commons-math3-*.jar /opt/jackcess/ && \
    cp /tmp/m2/commons-io/commons-io/*/commons-io-*.jar /opt/jackcess/ && \
    cp /tmp/m2/com/zaxxer/SparseBitSet/*/SparseBitSet-*.jar /opt/jackcess/ && \
    cp /tmp/m2/org/apache/logging/log4j/log4j-api/*/log4j-api-*.jar /opt/jackcess/


FROM ${BASE_IMAGE} AS docling-base

###################################################################################################
# OS Layer                                                                                        #
###################################################################################################

USER 0

RUN --mount=type=bind,source=os-packages.txt,target=/tmp/os-packages.txt \
    dnf -y install --best --nodocs --setopt=install_weak_deps=False dnf-plugins-core && \
    dnf config-manager --best --nodocs --setopt=install_weak_deps=False --save && \
    dnf config-manager --enable crb && \
    dnf -y update && \
    dnf install -y $(cat /tmp/os-packages.txt) && \
    dnf -y clean all && \
    rm -rf /var/cache/dnf

# Legacy .doc/.xls/.ppt ingestion requires a resolvable distro launcher plus
# util-linux prlimit for the kernel-enforced RLIMIT_FSIZE boundary.
RUN test -x "$(readlink -f "$(command -v soffice)")" && \
    prlimit --version && \
    "$(readlink -f "$(command -v soffice)")" --headless --version

COPY --from=mimalloc /opt/app-root/src/mimalloc/out/release/libmimalloc.so /usr/local/lib/libmimalloc.so

# libngspice (+ its versioned symlinks) for PySpice's NgSpiceShared binding.
# Register /usr/local/lib so the dynamic loader (and ctypes find_library) resolve it.
COPY --from=ngspice /usr/local/lib/libngspice.so* /usr/local/lib/
COPY --from=jackcess /opt/jackcess /opt/jackcess
RUN echo "/usr/local/lib" > /etc/ld.so.conf.d/local-lib.conf && ldconfig

RUN /usr/bin/fix-permissions /opt/app-root/src/.cache

ENV TESSDATA_PREFIX=/usr/share/tesseract/tessdata/

FROM ${UV_IMAGE} AS uv_stage

###################################################################################################
# Docling layer                                                                                   #
###################################################################################################

FROM docling-base

USER 1001

WORKDIR /opt/app-root/src

ENV \
    OMP_NUM_THREADS=4 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/app-root \
    DOCLING_SERVE_JACKCESS_CLASSPATH=/opt/jackcess/* \
    DOCLING_SERVE_ARTIFACTS_PATH=/opt/app-root/src/.cache/docling/models \
    HF_HOME=/opt/app-root/src/.cache/huggingface

ARG UV_SYNC_EXTRA_ARGS

RUN --mount=from=uv_stage,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/opt/app-root/src/.cache/uv,uid=1001 \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    umask 002 && \
    UV_SYNC_ARGS="--frozen --no-install-project --no-dev --all-extras" && \
    uv sync ${UV_SYNC_ARGS} ${UV_SYNC_EXTRA_ARGS} --no-extra flash-attn && \
    FLASH_ATTENTION_SKIP_CUDA_BUILD=TRUE uv sync ${UV_SYNC_ARGS} ${UV_SYNC_EXTRA_ARGS} --no-build-isolation-package=flash-attn

# Baked models cover the full native enrichment set used for max extraction:
# layout + tableformer (tables), picture_classifier (image classification),
# code_formula (code + formula enrichment), and the OCR engines. Picture
# *description* (captions) uses the remote LiteLLM/Bedrock API, not a local VLM,
# so no captioning model is baked. Add more here if new enrichments are enabled.
ARG MODELS_LIST="layout tableformer picture_classifier code_formula rapidocr easyocr"
# The HybridChunker tokenizer is not a docling model (docling-tools can't fetch
# it) and is otherwise pulled from HuggingFace lazily on first chunk request —
# forbidden in an air-gapped / IL5 runtime. Bake the FULL model repo here (not
# just AutoTokenizer): the chunker reads sentence_bert_config.json to determine
# max_tokens, which AutoTokenizer.from_pretrained does NOT cache — so an offline
# runtime fails chunking with "max_tokens could not be determined" without it.
ARG CHUNK_TOKENIZER_MODEL="sentence-transformers/all-MiniLM-L6-v2"

# Only build-time stage allowed to reach HuggingFace: pull every docling model
# AND pre-cache the chunker tokenizer into HF_HOME. After this the image is
# sealed offline (see ENV below).
#
# Mount-bind a file containing "0" over /proc/sys/crypto/fips_enabled so that
# PyTorch's bundled OpenSSL sees FIPS as disabled and skips its self-test on a
# FIPS-enabled build runner. --security=insecure grants CAP_SYS_ADMIN and must
# run as root for mount(2) to succeed (build with BuildKit insecure entitlement).
USER 0
RUN --security=insecure \
    bash -c 'set -e; \
        printf "0\n" > /tmp/fips_zero; \
        mount --bind /tmp/fips_zero /proc/sys/crypto/fips_enabled; \
        echo "Downloading docling models..."; \
        HF_HUB_DOWNLOAD_TIMEOUT="90" HF_HUB_ETAG_TIMEOUT="90" \
        docling-tools models download -o "$DOCLING_SERVE_ARTIFACTS_PATH" $MODELS_LIST; \
        echo "Pre-caching chunker tokenizer model ($CHUNK_TOKENIZER_MODEL)..."; \
        HF_HUB_DOWNLOAD_TIMEOUT="90" HF_HUB_ETAG_TIMEOUT="90" \
        python -c "from huggingface_hub import snapshot_download; snapshot_download(\"$CHUNK_TOKENIZER_MODEL\")"' && \
    chown -R 1001:0 ${DOCLING_SERVE_ARTIFACTS_PATH} ${HF_HOME} && \
    chmod -R g=u ${DOCLING_SERVE_ARTIFACTS_PATH} ${HF_HOME}
USER 1001

COPY --chown=1001:0 ./docling_serve ./docling_serve
COPY --chown=1001:0 ./scripts/smoke_legacy_office_runtime.py ./scripts/smoke_legacy_office_runtime.py
COPY --chown=1001:0 ./scripts/smoke_jackcess_runtime.py ./scripts/smoke_jackcess_runtime.py
COPY --chown=1001:0 ./production.yaml ./production.yaml

RUN --mount=from=uv_stage,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/opt/app-root/src/.cache/uv,uid=1001 \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    umask 002 && uv sync --frozen --no-dev --all-extras ${UV_SYNC_EXTRA_ARGS}

ENV LD_PRELOAD=/usr/local/lib/libmimalloc.so

# Point PySpice's NgSpiceShared binding straight at the baked shared library
# (it honours NGSPICE_LIBRARY_PATH before falling back to ctypes find_library).
ENV NGSPICE_LIBRARY_PATH=/usr/local/lib/libngspice.so

# === Air-gapped / IL5 lockdown ===========================================
# All models + the chunker tokenizer are baked above. Force the HuggingFace /
# transformers / datasets stacks fully offline so the running container can
# never attempt an outbound fetch; a missing artifact fails fast instead of
# hanging on a firewall-blocked call. To enable an enrichment needing an extra
# model, add it to MODELS_LIST at build time — never relax these flags.
ENV \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    DOCLING_SERVE_CONFIG_FILE=/opt/app-root/src/production.yaml

EXPOSE 5001

CMD ["docling-serve", "run"]
