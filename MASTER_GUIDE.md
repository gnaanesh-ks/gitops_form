# Master Deployment Guide: Two-Tier App on EKS (Flask + PostgreSQL)

**Architecture**
```
Developer -> Git repo -> ArgoCD (watches repo) -> EKS cluster
                                                     ├── Deployment: Flask app (pulls image from ECR)
                                                     └── StatefulSet: PostgreSQL (persistent EBS volume)
Prometheus (kube-prometheus-stack) scrapes /metrics -> Grafana dashboards
```

Companion files in this project:
```
app/                          Flask source + Dockerfile
helm/two-tier-app/            Helm chart (Deployment, Service, StatefulSet, Secret, ConfigMap, ServiceMonitor)
argocd/application.yaml       ArgoCD Application (GitOps pointer)
monitoring/prometheus-values.yaml   values for kube-prometheus-stack
```

---

## Phase 0 — Prerequisites

Install locally: `aws` CLI (v2), `kubectl`, `eksctl`, `helm` (v3), `docker`, `git`, `argocd` CLI (optional).

```bash
aws configure                     # set access key, secret, region
aws sts get-caller-identity       # confirm identity/account
```

---

## Phase 0.5 — Run the App Locally Before Touching Kubernetes

Test the Flask + Postgres pair with Docker Compose first, on your EC2 instance (or laptop) — this validates the app code and Dockerfile independent of any cluster.

```bash
cd two-tier-app
docker compose up --build
```

This builds the Flask image locally and starts both containers, with Postgres healthchecked before Flask starts.

Verify:
```bash
curl http://localhost:5000/healthz
curl http://localhost:5000/readyz     # should report "ready" once DB is reachable
```

If you're on an EC2 instance and browsing from your own laptop, either:
- open the EC2 security group's port 5000 temporarily to your IP, or
- SSH tunnel instead: `ssh -L 5000:localhost:5000 ec2-user@<ec2-public-ip>`, then browse `http://localhost:5000/register` locally.

Submit a test registration, then confirm it landed in Postgres:
```bash
docker exec -it local-postgres psql -U appuser -d appdb -c "SELECT * FROM registrations;"
```

Tear down when done:
```bash
docker compose down          # add -v to also delete the local pgdata volume
```

Only move to Phase 1 (EKS) once this local run behaves correctly — it isolates app bugs from cluster/networking issues.

---

## Phase 1 — Provision the EKS Cluster

```bash
eksctl create cluster \
  --name two-tier-cluster \
  --region us-east-1 \
  --version 1.30 \
  --nodegroup-name standard-workers \
  --node-type t3.medium \
  --nodes 3 --nodes-min 2 --nodes-max 5 \
  --managed

# Point kubectl at the new cluster
aws eks update-kubeconfig --name two-tier-cluster --region us-east-1
kubectl get nodes
```

Ensure the EBS CSI driver add-on is installed (needed for the StatefulSet's persistent volume):
```bash
eksctl create addon --cluster two-tier-cluster --name aws-ebs-csi-driver --region us-east-1
```

---

## Phase 2 — Build the Image and Push to ECR

```bash
# 1. Create the ECR repository
aws ecr create-repository --repository-name two-tier-flask-app --region us-east-1

# 2. Authenticate Docker to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

# 3. Build and tag
cd app
docker build -t two-tier-flask-app:1.0.0 .
docker tag two-tier-flask-app:1.0.0 <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/two-tier-flask-app:1.0.0

# 4. Push
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/two-tier-flask-app:1.0.0
```

Update `helm/two-tier-app/values.yaml` → `flask.image.repository` and `flask.image.tag` with your real account ID/region/tag, then commit.

**Node IAM permissions**: the EKS worker node role needs `AmazonEC2ContainerRegistryReadOnly` attached so pods can pull from ECR.

---

## Phase 3 — Package the Helm Chart

```bash
cd helm/two-tier-app
helm lint .                          # validate chart syntax
helm template . --values values.yaml # render manifests locally to sanity-check output
helm package .                       # produces two-tier-app-0.1.0.tgz (optional, for a chart repo)
```

Push the chart source (not just the .tgz) to your Git repository — ArgoCD will read directly from the `helm/two-tier-app` path.

**Secrets note**: `values.yaml` currently has a placeholder DB password in plaintext for simplicity. For production, replace `db.password` with a reference resolved by External Secrets Operator, Sealed Secrets, or SOPS, so no plaintext secret is committed to Git.

---

## Phase 4 — Install Argo CD on the Cluster

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for pods to be ready
kubectl get pods -n argocd -w

# Get initial admin password
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d

# Expose the UI/API via a public AWS LoadBalancer (instead of port-forward)
kubectl apply -f argocd/argocd-server-loadbalancer.yaml

# Wait for the ELB to provision, then get its public hostname
kubectl get svc argocd-server -n argocd -w
# EXTERNAL-IP column shows a *.elb.amazonaws.com hostname once ready

# Browse to https://<EXTERNAL-IP-HOSTNAME>  (self-signed cert warning is expected
# unless you've configured a real TLS cert - click through it)
```

Optionally log in via CLI:
```bash
argocd login localhost:8080 --username admin --password <PASSWORD>
```

---

## Phase 5 — Register the App with Argo CD (GitOps)

Edit `argocd/application.yaml`:
- `spec.source.repoURL` → your Git repo URL
- `spec.source.targetRevision` → branch (e.g. `main`)
- `spec.source.path` → `helm/two-tier-app`

Apply it:
```bash
kubectl apply -f argocd/application.yaml
```

Or register via CLI instead of a manifest:
```bash
argocd app create two-tier-app \
  --repo https://github.com/<your-org>/<your-repo>.git \
  --path helm/two-tier-app \
  --dest-server https://kubernetes.default.svc \
  --dest-namespace two-tier-app \
  --sync-policy automated \
  --auto-prune --self-heal
```

Argo CD will now:
1. Clone the repo and render the Helm chart.
2. Create the `two-tier-app` namespace.
3. Apply the Deployment, Service, Secret, ConfigMap, and StatefulSet.
4. Continuously reconcile — any drift or manual `kubectl edit` gets reverted; any new Git commit gets auto-synced.

Verify:
```bash
argocd app get two-tier-app
kubectl get all -n two-tier-app
kubectl get pvc -n two-tier-app     # confirm the Postgres EBS volume was provisioned
```

---

## Phase 6 — Install Prometheus and Grafana

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring

helm install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring \
  -f monitoring/prometheus-values.yaml
```

This installs Prometheus, Alertmanager, Grafana, and the Prometheus Operator (which watches for `ServiceMonitor` custom resources).

Because the Flask app's `prometheus-flask-exporter` library exposes `/metrics`, and the Helm chart includes a `ServiceMonitor` (`flask-servicemonitor.yaml`) labeled `release: prometheus`, the Operator auto-discovers it — no manual Prometheus config edits needed.

Access Grafana:
```bash
kubectl get svc prometheus-grafana -n monitoring -w
# EXTERNAL-IP column shows a *.elb.amazonaws.com hostname once the ELB provisions

# Browse to http://<EXTERNAL-IP-HOSTNAME>  (default user: admin / password from values.yaml)
```

In Grafana: **Dashboards → Import** a Flask/Gunicorn dashboard (e.g. ID 12708 or 11378 on grafana.com) or build a panel querying `flask_http_request_duration_seconds_count` / `flask_http_request_total`.

---

## Phase 7 — Validate End to End

```bash
# Port-forward the Flask service and hit the registration form
kubectl port-forward svc/flask-app -n two-tier-app 8000:80
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
# Open http://localhost:8000/register in a browser and submit a test entry

# Confirm the row landed in Postgres
kubectl exec -it postgres-0 -n two-tier-app -- \
  psql -U appuser -d appdb -c "SELECT * FROM registrations;"

# Confirm Prometheus is scraping the target
kubectl port-forward svc/prometheus-operated -n monitoring 9090:9090
# Browse to http://localhost:9090/targets and look for the flask-app job as "UP"
```

---

## Phase 8 — Day-2 Operations

| Task | How |
|---|---|
| Ship a new app version | Build/push new image tag to ECR → bump `flask.image.tag` in `values.yaml` → commit → Argo CD auto-syncs |
| Roll back | `argocd app rollback two-tier-app <REVISION>`, or `git revert` the values change |
| Scale Flask | Change `flask.replicaCount` in `values.yaml` (or add an HPA manifest) → commit |
| Scale/back up Postgres storage | Adjust `postgres.storage`; note StatefulSet volume expansion needs the StorageClass to allow it |
| Inspect sync drift | `argocd app diff two-tier-app` |
| Alerting | Add `PrometheusRule` CRs (e.g., alert if `up{job="flask-app"} == 0`) via Alertmanager config |

---

## Key Design Notes

- **StatefulSet vs Deployment for Postgres**: StatefulSet gives Postgres a stable network identity (`postgres-0`) and a dedicated PersistentVolumeClaim per pod via `volumeClaimTemplates`, so data survives pod rescheduling — a plain Deployment would not guarantee this.
- **Headless Service**: `clusterIP: None` on the Postgres Service is required for StatefulSet DNS (`postgres-0.postgres.<ns>.svc.cluster.local`).
- **Readiness vs liveness**: `/readyz` checks DB connectivity so Kubernetes won't route traffic to a Flask pod that can't reach Postgres yet; `/healthz` only checks the process is alive, avoiding restart loops during transient DB hiccups.
- **GitOps boundary**: Argo CD's `selfHeal: true` means the cluster state is a reflection of Git — manual `kubectl` edits inside the `two-tier-app` namespace will be reverted, which is intentional for auditability.
- **Public LoadBalancers for Grafana/Argo CD**: both now provision internet-facing AWS ELBs with no IP restriction by default. That's convenient for testing from an EC2 terminal, but before leaving this running, lock it down — either add `loadBalancerSourceRanges` to each Service (restrict to your office/VPN CIDR), switch to an internal NLB, or put them behind the same ALB+auth you'd use for the app. Both still require login (Argo CD password, Grafana admin/changeme) but a public IP with only a password is a soft target — change the default Grafana password immediately.
