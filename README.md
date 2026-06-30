# MLOps Platform Assignment

A local GitOps platform.
Deploys a CPU-burning workload on Kubernetes using FluxCD for GitOps reconciliation,
ingress-nginx for routing with HTTP Basic Auth protected by SOPS-encrypted secrets,
and an HPA autoscaler validated by a purpose-built load testing CLI.

[![Validate](https://github.com/daviDpaD18/mlops-platform-assignment/actions/workflows/validate.yaml/badge.svg)](https://github.com/daviDpaD18/mlops-platform-assignment/actions/workflows/validate.yaml)


## Setup

All cluster operations, Flux lifecycle commands, and load testing are wrapped in a 
[Taskfile](https://taskfile.dev) — an alternative to Makefiles that makes the 
entire platform reproducible with short, self-documenting commands. Run `task --list` to see 
all available commands. After copying the repo be carefull to update your GITHUB_USER in the Taskfile.

```bash
task --list
```

```
cluster:up        Create local k3d cluster
cluster:down      Delete local k3d cluster
cluster:reset     Kill cluster and rebuild it from scratch
flux:bootstrap    Bootstrap FluxCD from this repo
flux:reconcile    Force immediate Flux sync across layers
flux:status       Check Flux reconciliation status
validate          Run CI checks locally before pushing
status            Full platform health check
test:endpoints    Validate auth and routing end to end
burnctl:reset     Scale to 1 pod for a clean load test baseline
burnctl:run       Run load test (pass args with --)
sops:setup        Generate age keypair and configure SOPS
sops:secret:apply Push age private key to cluster for SOPS decryption
```

### Prerequisites

| Tool | Purpose | Install (macOS) | Install (Linux / Ubuntu) |
|------|---------|-----------------|--------------------------|
| [Docker](https://docs.docker.com/get-docker/) | Container runtime for k3d | `brew install --cask docker` | `curl -fsSL https://get.docker.com \| sh` |
| [k3d](https://k3d.io) | Local Kubernetes cluster | `brew install k3d` | `curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh \| bash` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Kubernetes CLI | `brew install kubectl` | `sudo snap install kubectl --classic` |
| [Flux CLI](https://fluxcd.io/flux/installation/) | GitOps operator | `brew install fluxcd/tap/flux` | `curl -s https://fluxcd.io/install.sh \| sudo bash` |
| [Taskfile](https://taskfile.dev) | Task runner | `brew install go-task` | `sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b /usr/local/bin` |
| [age](https://age-encryption.org) | Encryption for SOPS | `brew install age` | `sudo apt install age` |
| [SOPS](https://getsops.io) | Secret encryption | `brew install sops` | Download binary from [GitHub Releases](https://github.com/getsops/sops/releases) |

### Bootstrap

**1. Create the cluster:**
```bash
task cluster:up
```

**2. Bootstrap FluxCD** (requires a GitHub personal access token with `repo` scope exported as `GITHUB_TOKEN`):
```bash
task flux:bootstrap
```

**3. Apply the SOPS age private key** so Flux can decrypt the basic-auth secret:
```bash
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey=age.key
```

**4. Verify everything is healthy:**
```bash
task status
```

Flux reconciles all infrastructure and application manifests automatically within a few minutes. No manual `kubectl apply` is needed after bootstrap.

### Run the load test

```bash
task burnctl:reset   # scale to 1 pod for a clean baseline
task burnctl:run -- --duration 120 --concurrency 15
```

A PNG chart is saved to `burnctl/results/` after each run.

### Validate endpoints manually

```bash
task test:endpoints
```

### Tear down

```bash
task cluster:down
```
## Architecture

```mermaid
flowchart LR
    DEV[Developer] --> REPO[GitHub repo]
    REPO --> CI[GitHub Actions]
    REPO --> REN[Renovate]
    REPO --> FLUX[FluxCD]
    FLUX --> NGINX[ingress-nginx]
    FLUX --> METRICS[metrics-server]
    FLUX --> PODS[loadtester pods]
    FLUX --> SECRET[basic-auth secret]
    SECRET --> NGINX
    METRICS --> HPA[HPA]
    HPA --> PODS
    NGINX --> PODS
    BURNCTL[burnctl] --> NGINX
```

## Workflow

The repository acts as the source of truth.
After inital Flux bootstrap Flux CD pools the repository every 5 minutes and reconciles
any drift between Git and what the cluster is running, so no manual `kubectl apply` is needed.

The repository is split into 2 layers. The `infrasturcture` folder contains the platform components
deployed as Helm charts - ingress-nginx for routing and HTTP Basic Auth, and metrics-server 
for resource monitoring. The `apps` folder has the loadtester workload as Kubernetes manifests (Deployment,
Service, Ingress, HPA, and encrypted Secret). FluxCD deploys the `infrastructure` layer first using a 
`dependsOn` constraint, because I want the ingress controller ready before the application manifests are applied.

The HTTP Basic Auth credentials are never stored in plaintext. The htpasswd-encoded 
secret is encrypted with SOPS and age before being committed to the public repository. 
Flux decrypts it automatically at reconciliation time using the age private key stored 
as a Kubernetes Secret in the cluster — the only piece of state that lives outside Git.

The Horizontal Pod Autoscaler watches both CPU and memory utilization across the 
loadtester pods. When the `/burn` endpoint is hit with concurrent requests, CPU 
climbs past the 50% threshold and the HPA scales the Deployment from 1 up to 5 
replicas. Once load drops and the stabilization window expires, it scales back down.

Every pull request runs through a three-job CI pipeline: yamllint validates manifest 
syntax, flux-local renders the full Flux object tree including HelmReleases, and a 
diff job posts a comment showing exactly what would change in the cluster if the PR 
were merged. Branch protection prevents anything from reaching `main` — and therefore 
Flux — unless all checks pass. Renovate runs automatically to keep Helm chart versions 
and GitHub Actions versions up to date, opening PRs that go through the same pipeline.

