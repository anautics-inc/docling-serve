# Enabling and Testing CUDA for Docling Serve on AWS EC2 (Amazon Linux 2023)

This guide covers the one-time host setup required for the NVIDIA T4 GPU on a
`g4dn.xlarge` EC2 instance to be accessible from the Docling Serve Docker
container, and the steps to verify that CUDA inference is working end-to-end.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| EC2 instance type | `g4dn.xlarge` (1× NVIDIA T4 16 GB, 4 vCPUs, 16 GB RAM) |
| AMI | Amazon Linux 2023 (AL2023) |
| Docker | Installed and running (`sudo systemctl status docker`) |
| Container image | Built with `UV_SYNC_EXTRA_ARGS=--no-group pypi --group cu128` (CUDA 12.8 PyTorch wheels) |
| CI/CD env file | `.env.g4dn-xlarge` with `DOCLING_DEVICE=cuda` |

---

## Step 1 — Install the NVIDIA Kernel Driver

The NVIDIA kernel module must be loaded on the **host** before any container
can access the GPU.

```bash
# Install build tools and DKMS
sudo dnf install -y kernel-devel kernel-headers gcc dkms

# Add the CUDA repository for AL2023 (RHEL9-compatible)
sudo dnf config-manager --add-repo \
  https://developer.download.nvidia.com/compute/cuda/repos/rhel9/x86_64/cuda-rhel9.repo

# Install the latest NVIDIA driver via DKMS
# This builds the kernel module against the running kernel.
sudo dnf install -y nvidia-driver-latest-dkms

# Reboot to load the new kernel module
sudo reboot
```

After the reboot, verify the driver is loaded:

```bash
nvidia-smi
```

Expected output (abridged):

```
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 570.x.x     Driver Version: 570.x.x     CUDA Version: 12.8                 |
|-----------------------------------------+------------------------+----------------------+
| GPU  Name        Persistence-M | ...                                                   |
|   0  Tesla T4              Off | ...                                                   |
```

> **Note:** The CUDA version shown by `nvidia-smi` reflects the maximum CUDA
> version the installed driver supports. The PyTorch CUDA 12.8 wheels
> (`cu128`) require driver ≥ 525.60.13.

---

## Step 2 — Install the NVIDIA Container Toolkit

The NVIDIA Container Toolkit is the bridge between the Docker daemon and the
host GPU driver. Without it, `--gpus all` has no effect.

```bash
# Add the NVIDIA Container Toolkit repository
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -fsSL \
  https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
  | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo

sudo dnf install -y nvidia-container-toolkit

# Register the NVIDIA runtime with Docker
sudo nvidia-ctk runtime configure --runtime=docker

# Restart Docker to pick up the new runtime
sudo systemctl restart docker
```

Confirm Docker now has the NVIDIA runtime:

```bash
docker info | grep -i nvidia
# Expected:  Runtimes: ... nvidia ...
#            Default Runtime: runc
```

---

## Step 3 — Verify GPU Access from a Bare Container

Before deploying Docling Serve, confirm the GPU is reachable from inside any
container:

```bash
docker run --rm --gpus all \
  nvidia/cuda:12.8.0-base-ubi9 \
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
```

Expected output:

```
Tesla T4, 570.x.x, 16106 MiB
```

If this step fails, the issue is with the host driver or NVIDIA Container
Toolkit installation — not with Docling Serve.

---

## Step 4 — Build and Deploy the CUDA-Enabled Container Image

The container image must be built with CUDA-enabled PyTorch wheels. The
GitLab CI/CD pipeline is already configured to do this via the global
variable:

```yaml
UV_SYNC_EXTRA_ARGS: "--no-group pypi --group cu128"
```

This passes `--build-arg UV_SYNC_EXTRA_ARGS` to `docker buildx build`, which
the `Containerfile` threads into both `uv sync` calls, replacing the default
CPU-only PyPI wheels with CUDA 12.8 wheels from the `pytorch-cu128` index.

**Important:** The default image (`ghcr.io/docling-project/docling-serve:main`)
is CPU-only. Only images built by this project's CI pipeline with the
`UV_SYNC_EXTRA_ARGS` variable set will contain CUDA-capable PyTorch.

Trigger a new pipeline run after any change to `.gitlab-ci.yml` or
`pyproject.toml` to produce an updated CUDA image.

---

## Step 5 — Deploy with GPU Flags

The CI/CD deploy script already passes `--gpus all` and
`DOCLING_DEVICE=cuda` to the container. For reference, the equivalent manual
`docker run` command is:

```bash
docker run \
  --name docling-serve \
  --restart always \
  --gpus all \
  --env-file /path/to/.env.g4dn-xlarge \
  --volume /mnt/docling/scratch:/mnt/docling/scratch \
  --publish 8000:5001 \
  --detach \
  <your-registry>/docling-serve:<tag>
```

Key environment variables in `.env.g4dn-xlarge` relevant to GPU:

| Variable | Value | Purpose |
|---|---|---|
| `DOCLING_DEVICE` | `cuda` | Routes all model inference to the T4 |
| `DOCLING_NUM_THREADS` | `4` | CPU threads for data loading (matches vCPU count) |
| `DOCLING_PERF_PAGE_BATCH_SIZE` | `4` | Pages fed to GPU per iteration |
| `DOCLING_PERF_ELEMENTS_BATCH_SIZE` | `8` | Elements processed per enrichment pass |
| `DOCLING_SERVE_LAYOUT_BATCH_SIZE` | `4` | Layout detection batch size |
| `DOCLING_SERVE_TABLE_BATCH_SIZE` | `4` | Table structure batch size |
| `DOCLING_SERVE_OCR_BATCH_SIZE` | `4` | OCR batch size |
| `DOCLING_SERVE_ENG_LOC_SHARE_MODELS` | `true` | Single copy of model weights across workers |

---

## Step 6 — Test CUDA from Inside the Running Container

### 6a. Verify PyTorch sees the GPU

```bash
docker exec docling-serve python - <<'EOF'
import torch
print("CUDA available :", torch.cuda.is_available())
print("Device count   :", torch.cuda.device_count())
if torch.cuda.is_available():
    print("Device name    :", torch.cuda.get_device_name(0))
    print("VRAM total     :", round(torch.cuda.get_device_properties(0).total_memory / 1e9, 2), "GB")
EOF
```

Expected output:

```
CUDA available : True
Device count   : 1
Device name    : Tesla T4
VRAM total     : 16.1 GB
```

If `CUDA available` is `False`, the image was built with CPU wheels — rebuild
the pipeline with `UV_SYNC_EXTRA_ARGS: "--no-group pypi --group cu128"`.

### 6b. Verify VRAM is being used during inference

In one terminal, watch GPU memory:

```bash
watch -n2 "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.free --format=csv,noheader"
```

In another terminal, send a conversion request:

```bash
curl -s -X POST "http://localhost:8000/v1/convert/source" \
  -H "Content-Type: application/json" \
  -d '{"sources": [{"kind": "http", "url": "https://arxiv.org/pdf/2501.17887"}]}' \
  | python -m json.tool | head -20
```

During processing, `memory.used` should rise by several hundred MiB to a few
GiB depending on document complexity. If it stays at the idle baseline
(~150 MiB for the driver), inference is happening on CPU despite the device
setting.

### 6c. Check application logs for the active device

```bash
docker logs docling-serve 2>&1 | grep -i "device\|cuda\|gpu" | head -20
```

Look for log lines like:

```
Using device: cuda
Model loaded on cuda:0
```

### 6d. Health probe

```bash
curl -s http://localhost:8000/health | python -m json.tool
# Should return {"status": "ok", ...}
```

---

## Troubleshooting

### `CUDA available: False` inside the container

**Cause:** The container image contains CPU-only PyTorch wheels.

**Fix:** Ensure the CI pipeline variable `UV_SYNC_EXTRA_ARGS` is set to
`--no-group pypi --group cu128` and retrigger the build job. Confirm the
correct wheels were installed:

```bash
docker exec docling-serve pip show torch | grep -i "version\|location"
# Version should be e.g. 2.7.1+cu128
```

### `nvidia-smi` works on host but container sees no GPU

**Cause:** NVIDIA Container Toolkit is not registered with Docker.

**Fix:**
```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker info | grep -i nvidia   # must show 'nvidia' under Runtimes
```

### `docker run --gpus all` returns: `docker: Error response from daemon: could not select device driver "nvidia"`

**Cause:** `nvidia-container-toolkit` is installed but `nvidia-ctk runtime configure` was not run, or Docker was not restarted.

**Fix:** Re-run configure and restart:
```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### GPU utilisation stays at 0% during inference

**Cause:** `DOCLING_DEVICE` env var is not being passed into the container, or
the model pipeline is defaulting to CPU due to a CUDA initialisation error
(check `docker logs docling-serve` for stack traces).

**Fix:**
```bash
# Confirm the variable is present inside the container
docker exec docling-serve printenv DOCLING_DEVICE
# Must print: cuda

# Check for CUDA errors in logs
docker logs docling-serve 2>&1 | grep -i "error\|warn" | head -40
```

### Out of memory (OOM) during inference

The T4 has 16 GB VRAM. With `ENG_LOC_SHARE_MODELS=true` and `OPTIONS_CACHE_SIZE=2`, 
peak VRAM usage should stay well under 16 GB for most documents. If OOM
errors appear in logs:

- Reduce `DOCLING_SERVE_LAYOUT_BATCH_SIZE`, `DOCLING_SERVE_TABLE_BATCH_SIZE`,
  and `DOCLING_SERVE_OCR_BATCH_SIZE` to `2`.
- Reduce `DOCLING_SERVE_OPTIONS_CACHE_SIZE` to `1`.
- Reduce `DOCLING_SERVE_ENG_LOC_NUM_WORKERS` to `1`.

---

## Summary Checklist

- [ ] NVIDIA driver installed and `nvidia-smi` works on the host
- [ ] NVIDIA Container Toolkit installed and registered with Docker
- [ ] `docker info` shows `nvidia` under Runtimes
- [ ] Bare container GPU test passes (`nvidia/cuda` image)
- [ ] CI pipeline built image with `UV_SYNC_EXTRA_ARGS: "--no-group pypi --group cu128"`
- [ ] Container deployed with `--gpus all`
- [ ] `DOCLING_DEVICE=cuda` present in env file
- [ ] `torch.cuda.is_available()` returns `True` inside the container
- [ ] VRAM usage increases during a conversion request
