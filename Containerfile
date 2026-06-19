ARG BASE_IMAGE=quay.io/sclorg/python-312-c9s:c9s

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.19

ARG UV_SYNC_EXTRA_ARGS=""

ARG MIMALLOC_VERSION=v3.2.8


###################################################################################################
# Build mimalloc                                                                                  #
###################################################################################################

FROM ${BASE_IMAGE} AS mimalloc

ARG MIMALLOC_VERSION

USER 0

RUN dnf install -y --best --nodocs --setopt=install_weak_deps=False gcc gcc-c++ make cmake git

RUN git clone --depth 1 --branch ${MIMALLOC_VERSION} https://github.com/microsoft/mimalloc.git /opt/app-root/src/mimalloc

WORKDIR /opt/app-root/src/mimalloc

RUN mkdir -p out/release

WORKDIR /opt/app-root/src/mimalloc/out/release
RUN cmake ../.. && make


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

COPY --from=mimalloc /opt/app-root/src/mimalloc/out/release/libmimalloc.so /usr/local/lib/libmimalloc.so
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
# forbidden in an air-gapped / IL5 runtime. Bake it into the image here.
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
        echo "Pre-caching chunker tokenizer ($CHUNK_TOKENIZER_MODEL)..."; \
        HF_HUB_DOWNLOAD_TIMEOUT="90" HF_HUB_ETAG_TIMEOUT="90" \
        python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained(\"$CHUNK_TOKENIZER_MODEL\")"' && \
    chown -R 1001:0 ${DOCLING_SERVE_ARTIFACTS_PATH} ${HF_HOME} && \
    chmod -R g=u ${DOCLING_SERVE_ARTIFACTS_PATH} ${HF_HOME}
USER 1001

COPY --chown=1001:0 ./docling_serve ./docling_serve

RUN --mount=from=uv_stage,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/opt/app-root/src/.cache/uv,uid=1001 \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    umask 002 && uv sync --frozen --no-dev --all-extras ${UV_SYNC_EXTRA_ARGS}

ENV LD_PRELOAD=/usr/local/lib/libmimalloc.so

# === Air-gapped / IL5 lockdown ===========================================
# All models + the chunker tokenizer are baked above. Force the HuggingFace /
# transformers / datasets stacks fully offline so the running container can
# never attempt an outbound fetch; a missing artifact fails fast instead of
# hanging on a firewall-blocked call. To enable an enrichment needing an extra
# model, add it to MODELS_LIST at build time — never relax these flags.
ENV \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

EXPOSE 5001

CMD ["docling-serve", "run"]
